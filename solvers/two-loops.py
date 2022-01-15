
from benchopt import BaseSolver
from benchopt.stopping_criterion import SufficientProgressCriterion

from benchopt import safe_import_context

with safe_import_context() as import_ctx:
    import numpy as np
    from numba import njit
    constants = import_ctx.import_from('constants')
    sgd_inner = import_ctx.import_from('sgd_inner', 'sgd_inner')
    MinibatchSampler = import_ctx.import_from(
        'minibatch_sampler', 'MinibatchSampler'
    )
    LearningRateScheduler = import_ctx.import_from(
        'learning_rate_scheduler', 'LearningRateScheduler'
    )


class Solver(BaseSolver):
    """Two loops solver."""
    name = 'two-loops'

    stopping_criterion = SufficientProgressCriterion(
        patience=constants.PATIENCE, strategy='callback'
    )

    # any parameter defined here is accessible as a class attribute
    parameters = {
        'n_inner_step': constants.N_INNER_STEPS,
        'batch_size': constants.BATCH_SIZES,
        'step_size': constants.STEP_SIZES,
        'outer_ratio': constants.OUTER_RATIOS,
    }

    @staticmethod
    def get_next(stop_val):
        return stop_val + 1

    def set_objective(self, f_train, f_test, inner_var0, outer_var0):
        self.f_inner = f_train
        self.f_outer = f_test
        self.inner_var0 = inner_var0
        self.outer_var0 = outer_var0

        if self.batch_size == 'all':
            self.inner_batch_size = self.f_inner.n_samples
            self.outer_batch_size = self.f_outer.n_samples
        else:
            self.inner_batch_size = self.batch_size
            self.outer_batch_size = self.batch_size

    def run(self, callback):
        eval_freq = constants.EVAL_FREQ
        rng = np.random.RandomState(constants.RANDOM_STATE)

        # Init variables
        outer_var = self.outer_var0.copy()
        inner_var = self.inner_var0.copy()
        inner_sampler = MinibatchSampler(
            self.f_inner.n_samples, self.inner_batch_size
        )
        outer_sampler = MinibatchSampler(
            self.f_outer.n_samples, self.outer_batch_size
        )
        step_sizes = np.array(
            [self.step_size, self.step_size / self.outer_ratio]
        )
        exponents = np.zeros(2)
        lr_scheduler = LearningRateScheduler(
            np.array(step_sizes, dtype=float), exponents
        )

        callback((inner_var, outer_var))
        # L = self.f_inner.lipschitz_inner(inner_var, outer_var)
        inner_var = sgd_inner(
            self.f_inner.numba_oracle, inner_var, outer_var,
            step_size=self.step_size,
            inner_sampler=inner_sampler, n_inner_step=self.n_inner_step
        )
        while callback((inner_var, outer_var)):
            inner_var, outer_var = two_loops(
                self.f_inner.numba_oracle, self.f_outer.numba_oracle,
                inner_var, outer_var, eval_freq, self.n_inner_step,
                inner_sampler, outer_sampler, lr_scheduler,
                seed=rng.randint(constants.MAX_SEED)
            )

        self.beta = (inner_var, outer_var)

    def get_result(self):
        return self.beta


@njit
def two_loops(inner_oracle, outer_oracle, inner_var, outer_var,
              max_iter, n_inner_step, inner_sampler, outer_sampler,
              lr_scheduler, seed=None):

    # Set seed for randomness
    if seed is not None:
        np.random.seed(seed)

    for i in range(max_iter):
        inner_lr, outer_lr = lr_scheduler.get_lr()
        outer_slice, _ = outer_sampler.get_batch()
        grad_in, grad_out = outer_oracle.grad(
            inner_var, outer_var, outer_slice
        )

        inner_slice, _ = inner_sampler.get_batch()
        _, _, _, implicit_grad = inner_oracle.oracles(
            inner_var, outer_var, grad_in, inner_slice, inverse='cg'
        )
        grad_outer_var = grad_out - implicit_grad

        outer_var -= outer_lr * grad_outer_var
        inner_var, outer_var = inner_oracle.prox(inner_var, outer_var)

        inner_var = sgd_inner(
            inner_oracle, inner_var, outer_var, step_size=inner_lr,
            inner_sampler=inner_sampler, n_inner_step=n_inner_step
        )
    return inner_var, outer_var
