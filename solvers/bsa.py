from benchopt import BaseSolver
from benchopt.stopping_criterion import SufficientProgressCriterion

from benchopt import safe_import_context

with safe_import_context() as import_ctx:
    import numpy as np
    from numba import njit
    from numba.experimental import jitclass

    from benchmark_utils import constants
    from benchmark_utils.minibatch_sampler import init_sampler
    from benchmark_utils.learning_rate_scheduler import update_lr
    from benchmark_utils.minibatch_sampler import MinibatchSampler
    from benchmark_utils.minibatch_sampler import spec as mbs_spec
    from benchmark_utils.sgd_inner import sgd_inner, sgd_inner_jax
    from benchmark_utils.hessian_approximation import hia, hia_jax
    from benchmark_utils.learning_rate_scheduler import init_lr_scheduler
    from benchmark_utils.learning_rate_scheduler import spec as sched_spec
    from benchmark_utils.learning_rate_scheduler import LearningRateScheduler
    from benchmark_utils.oracles import MultiLogRegOracle, DataCleaningOracle

    import jax
    import jax.numpy as jnp
    from functools import partial


class Solver(BaseSolver):
    """BSA - Two loops solver."""
    name = 'BSA'

    stopping_criterion = SufficientProgressCriterion(
        patience=constants.PATIENCE, strategy='callback'
    )

    # any parameter defined here is accessible as a class attribute
    parameters = {
        'step_size': [.1],
        'outer_ratio': [1.],
        'n_inner_steps': [10],
        'n_hia_steps': [10],
        'batch_size': [64],
        'eval_freq': [128],
        'random_state': [1],
        'framework': ['jax']
    }

    @staticmethod
    def get_next(stop_val):
        return stop_val + 1

    def skip(self, f_train, f_val, **kwargs):
        if self.framework == 'numba':
            if self.batch_size == 'full':
                return True, "Numba is not useful for full bach resolution."
            elif isinstance(f_train(),
                            (MultiLogRegOracle, DataCleaningOracle)):
                return True, "Numba implementation not available for " \
                      "this oracle."
            elif isinstance(f_val(), (MultiLogRegOracle, DataCleaningOracle)):
                return True, "Numba implementation not available for" \
                      "this oracle."
        elif self.framework not in ['jax', 'none', 'numba']:
            return True, f"Framework {self.framework} not supported."
        return False, None

    def set_objective(self, f_train, f_val, n_inner_samples, n_outer_samples,
                      inner_var0, outer_var0):
        self.f_inner = f_train(framework=self.framework)
        self.f_outer = f_val(framework=self.framework)
        self.n_inner_samples = n_inner_samples
        self.n_outer_samples = n_outer_samples

        # Init sampler and lr scheduler
        if self.batch_size == "full":
            self.batch_size_inner = n_inner_samples
            self.batch_size_outer = n_outer_samples
        else:
            self.batch_size_inner = self.batch_size
            self.batch_size_outer = self.batch_size

        if self.framework == 'numba':
            # JIT necessary functions and classes
            self.hia = njit(hia)
            njit_bsa = njit(_bsa)
            self.sgd_inner = njit(sgd_inner)
            self.MinibatchSampler = jitclass(MinibatchSampler, mbs_spec)
            self.LearningRateScheduler = jitclass(
                LearningRateScheduler, sched_spec
            )

            def bsa(*args, **kwargs):
                return njit_bsa(self.sgd_inner, self.hia, *args, **kwargs)
            self.bsa = bsa
        elif self.framework == 'none':
            self.hia = hia
            self.sgd_inner = sgd_inner
            self.MinibatchSampler = MinibatchSampler
            self.LearningRateScheduler = LearningRateScheduler

            def bsa(*args, **kwargs):
                return _bsa(sgd_inner, hia, *args, **kwargs)
            self.bsa = bsa
        elif self.framework == 'jax':
            self.f_inner = jax.jit(
                partial(self.f_inner, batch_size=self.batch_size_inner)
            )
            self.f_outer = jax.jit(
                partial(self.f_outer, batch_size=self.batch_size_outer)
            )
            inner_sampler, self.state_inner_sampler \
                = init_sampler(n_samples=n_inner_samples,
                               batch_size=self.batch_size_inner)
            outer_sampler, self.state_outer_sampler \
                = init_sampler(n_samples=n_outer_samples,
                               batch_size=self.batch_size_outer)
            self.sgd_inner = partial(
                sgd_inner_jax,
                grad_inner=jax.grad(self.f_inner, argnums=0),
                sampler=inner_sampler
            )
            self.bsa = partial(
                bsa_jax,
                hia=hia_jax,
                sgd_inner=self.sgd_inner,
                inner_sampler=inner_sampler,
                outer_sampler=outer_sampler
            )
        else:
            raise ValueError(f"Framework {self.framework} not supported.")

        self.inner_var0 = inner_var0
        self.outer_var0 = outer_var0
        if self.framework == 'numba' or self.framework == 'jax':
            self.run_once(2)

    def run(self, callback):
        eval_freq = self.eval_freq

        # Init variables
        outer_var = self.outer_var0.copy()
        inner_var = self.inner_var0.copy()

        if self.framework == 'jax':
            step_sizes = jnp.array(
                [self.step_size, self.step_size,
                 self.step_size / self.outer_ratio]
            )
            exponents = jnp.array([.5, 0., .5])
            state_lr = init_lr_scheduler(step_sizes, exponents)

            # Start algorithm
            inner_var, self.state_inner_sampler = self.sgd_inner(
                inner_var, outer_var,
                self.state_inner_sampler, step_size=self.step_size,
                n_steps=self.n_inner_steps
            )
            carry = dict(
                state_lr=state_lr,
                state_inner_sampler=self.state_inner_sampler,
                state_outer_sampler=self.state_outer_sampler,
                key=jax.random.PRNGKey(self.random_state)
            )
        else:
            rng = np.random.RandomState(self.random_state)
            inner_sampler = self.MinibatchSampler(
                self.f_inner.n_samples, batch_size=self.batch_size_inner
            )
            outer_sampler = self.MinibatchSampler(
                self.f_outer.n_samples, batch_size=self.batch_size_outer
            )
            step_sizes = np.array(
                [self.step_size, self.step_size,
                 self.step_size / self.outer_ratio]
            )
            exponents = np.array([.5, 0., .5])
            lr_scheduler = self.LearningRateScheduler(
                np.array(step_sizes, dtype=float), exponents
            )

            # Start algorithm
            inner_var = self.sgd_inner(
                self.f_inner, inner_var, outer_var,
                step_size=self.step_size, sampler=inner_sampler,
                n_steps=self.n_inner_steps
            )
        while callback((inner_var, outer_var)):
            if self.framework == 'jax':
                inner_var, outer_var, carry = self.bsa(
                        self.f_inner, self.f_outer, inner_var, outer_var,
                        n_inner_steps=self.n_inner_steps,
                        n_hia_steps=self.n_hia_steps, max_iter=eval_freq,
                        **carry
                    )
            else:
                inner_var, outer_var, = self.bsa(
                    self.f_inner, self.f_outer, inner_var, outer_var,
                    lr_scheduler, inner_sampler, outer_sampler,
                    n_inner_steps=self.n_inner_steps,
                    n_hia_steps=self.n_hia_steps, max_iter=eval_freq,
                    seed=rng.randint(constants.MAX_SEED)
                )

        self.beta = (inner_var, outer_var)

    def get_result(self):
        return self.beta


def _bsa(sgd_inner, hia, inner_oracle, outer_oracle, inner_var, outer_var,
         lr_scheduler, inner_sampler, outer_sampler,
         n_inner_steps=1, n_hia_steps=1, max_iter=1, seed=None):
    """Numba compatible BSA algorithm.

    Parameters
    ----------
    inner_oracle, outer_oracle: NumbaOracle
        Inner and outer problem oracles used to compute gradients, etc...
    inner_var, outer_var: ndarray
        Current estimates of the inner and outer variables of the bi-level
        problem.
    max_iter: int
        Maximal number of iteration for the outer problem.
    outer_step_size: float
        Step size to update the outer variable.
    n_inner_step: int
        Maximal number of iteration for the inner problem.
    inner_step_size: float
        Step size to update the inner variable.
    n_hia_step: int
        Maximal number of iteration for the HIA problem.
    hia_step_size: float
        Step size for the HIA sub-routine.
    inner_sampler, outer_sampler: MinibatchSampler
        Sampler to get minibatch in a fast and efficient way for the inner and
        outer problems.
    """

    # Set seed for randomness
    if seed is not None:
        np.random.seed(seed)

    for i in range(max_iter):
        inner_lr, hia_lr, outer_lr = lr_scheduler.get_lr()

        outer_slice, _ = outer_sampler.get_batch()
        grad_in, grad_out = outer_oracle.grad(
            inner_var, outer_var, outer_slice
        )

        implicit_grad = hia(
            inner_oracle, inner_var, outer_var, grad_in, hia_lr,
            sampler=inner_sampler, n_steps=n_hia_steps
        )
        inner_slice, _ = inner_sampler.get_batch()
        implicit_grad = inner_oracle.cross(
            inner_var, outer_var, implicit_grad, inner_slice
        )
        grad_outer_var = grad_out - implicit_grad

        outer_var -= outer_lr * grad_outer_var

        inner_var = sgd_inner(
            inner_oracle, inner_var, outer_var, step_size=inner_lr,
            sampler=inner_sampler, n_steps=n_inner_steps
        )
    return inner_var, outer_var


@partial(jax.jit, static_argnums=(0, 1),
         static_argnames=('hia', 'sgd_inner', 'n_hia_steps', 'n_inner_steps',
                          'inner_sampler', 'outer_sampler', 'max_iter'))
def bsa_jax(f_inner, f_outer, inner_var, outer_var,
            state_inner_sampler=None, state_outer_sampler=None,
            state_lr=None, hia=None, sgd_inner=None, n_hia_steps=1,
            n_inner_steps=1, inner_sampler=None, outer_sampler=None, key=None,
            max_iter=1):
    grad_inner_fun = jax.grad(f_inner, argnums=0)
    grad_outer_fun = jax.grad(f_outer, argnums=(0, 1))

    def bsa_one_iter(carry, _):

        (inner_lr, hia_lr, outer_lr), carry['state_lr'] = update_lr(
            carry['state_lr']
        )

        start_outer, *_, carry['state_outer_sampler'] = outer_sampler(
            carry['state_outer_sampler']
        )
        grad_in, grad_out = grad_outer_fun(
            carry['inner_var'], carry['outer_var'], start_outer)

        implicit_grad, carry['key'], carry['state_inner_sampler'] = hia(
            carry['inner_var'], carry['outer_var'], grad_in,
            carry['state_inner_sampler'], hia_lr, n_steps=n_hia_steps,
            sampler=inner_sampler, key=carry['key'], grad_inner=grad_inner_fun
        )
        start_inner, *_, carry['state_inner_sampler'] = inner_sampler(
            carry['state_inner_sampler']
        )
        _, vjp_fun = jax.vjp(
            lambda x: grad_inner_fun(carry['inner_var'], x, start_inner),
            carry['outer_var']
        )
        implicit_grad = vjp_fun(implicit_grad)[0]
        grad_outer_var = grad_out - implicit_grad

        carry['outer_var'] -= outer_lr * grad_outer_var
        # inner_var, outer_var = inner_oracle.prox(inner_var, outer_var)

        carry['inner_var'], state_inner_sampler = sgd_inner(
            carry['inner_var'], carry['outer_var'],
            carry['state_inner_sampler'], step_size=inner_lr,
            n_steps=n_inner_steps
        )

        return carry, _

    init = dict(
        inner_var=inner_var, outer_var=outer_var, state_lr=state_lr,
        state_inner_sampler=state_inner_sampler,
        state_outer_sampler=state_outer_sampler, key=key
    )
    carry, _ = jax.lax.scan(
        bsa_one_iter,
        init=init,
        xs=None,
        length=max_iter,
    )
    return carry['inner_var'], carry['outer_var'], \
        {k: v for k, v in carry.items() if k not in ['inner_var', 'outer_var']}
