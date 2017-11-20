import tensorflow as tf
import numpy as np
import sys
import tensorflow.contrib.eager as tfe
tfe.enable_eager_execution()
from copy import deepcopy
from likelihoods import PoissonLike

"""
Class for Kronecker inference of GPs

"""

class KroneckerSolver:


    def __init__(self, mu, kernel, likelihood, X, y, tau=0.5, k_diag=None, mask=None, verbose=False):
        """

        Args:
            kernel (kernels.Kernel): kernel function to use for inference
            likelihood (likelihoods.Likelihood): likelihood of observations given function values
            X (np.array): data
            y (np.array): output
            tau (float): Newton line search hyperparam
        """

        self.kernel = kernel
        self.mu = mu
        self.likelihood = likelihood
        self.Ks = self.construct_Ks()
        self.K_eigs = [tf.self_adjoint_eig(K) for K in self.Ks]
        self.k_diag = k_diag
        self.mask = mask
        
        self.X = X
        self.y = y
        self.verbose = verbose

        self.alpha = tf.zeros(shape=[X.shape[0]], dtype = tf.float32)
        self.W = tf.zeros(shape = [X.shape[0]])
        self.grads = tf.zeros(shape = [X.shape[0]])

        self.opt = CGOptimizer()
        self.f = self.mu
        self.tau = tau
        self.grad_func = tfe.gradients_function(self.likelihood.log_like, [1])
        self.hess_func = tfe.gradients_function(self.grad_func, [1])



    def construct_Ks(self, kernel=None):
        """

        Returns: list of kernel evaluations (tf.Variable) at each dimension

        """
        if kernel == None:
            kernel = self.kernel

        Ks = [tfe.Variable(kernel.eval(np.expand_dims(np.unique(self.X[:, i]), 1)),
                            dtype=tf.float32) for i in range(self.X.shape[1])]

        if kernel == None:
            self.Ks = Ks

        return Ks

    def step(self, max_it, it, prev, delta, verbose = False):
        """
        Runs one step of Kronecker inference
        Args:
            mu (tf.Variable): mean of f at each point x_i
            max_it (int): maximum number of Kronecker iterations
            it (int): current iteration
            f (tf.Variable): current estimate of function values
            delta (tf.Variable): change in step size from previous iteration

        Returns: mean, max iteration, current iteration, function values, and step size

        """

        self.f = kron_mvp(self.Ks, self.alpha) + self.mu

        if self.k_diag is not None:
            self.f += tf.multiply(self.alpha, self.k_diag)

        if self.mask is not None:
            y_lim = tf.boolean_mask(self.y, self.mask)
            f_lim = tf.boolean_mask(self.f, self.mask)
            alpha_lim = tf.boolean_mask(self.alpha, self.mask)
            mu_lim = tf.boolean_mask(self.mu, self.mask)
            psi = -tf.reduce_sum(self.likelihood.log_like(y_lim, f_lim)) + \
                  0.5 * tf.reduce_sum(tf.multiply(alpha_lim, f_lim - mu_lim))
        else:
            psi = -tf.reduce_sum(self.likelihood.log_like(self.y, self.f)) +\
              0.5*tf.reduce_sum(tf.multiply(self.alpha, self.f-self.mu))

        if self.verbose:
            print "Iteration: ", it
            print " psi: ", psi


        self.grads = self.grad_func(self.y, self.f)[0]
        hess = self.hess_func(self.y, self.f)[0]
        self.W = -hess


        b = tf.multiply(self.W, self.f - self.mu) + self.grads

        if self.k_diag is not None:
            z = self.opt.cg(tf.multiply(1.0/tf.sqrt(self.W), b), precondition= self.k_diag)
        else:
            z = self.opt.cg(tf.multiply(1.0/tf.sqrt(self.W), b))

        delta_alpha = tf.multiply(tf.sqrt(self.W), z) - self.alpha

        ls = self.line_search(self.alpha, delta_alpha, self.y, psi, 20, self.mu)
        step_size = ls[1]

        if self.verbose:
            print "step", step_size
            print ""

        delta = prev - psi
        prev = psi
        self.alpha = tf.cond(tf.greater(delta, 1e-5), lambda: self.alpha + delta_alpha*step_size, lambda: self.alpha)
        self.alpha = tf.where(tf.is_nan(self.alpha), tf.ones_like(self.alpha) * 1e-9, self.alpha)
        it = it + 1

        return max_it, it, prev, delta


    def conv(self, max_it, it, prev, delta):
        """
        Assesses convergence of Kronecker inference
        Args:
            mu (tf.Variable): mean of f at each point x_i
            max_it (int): maximum number of Kronecker iterations
            it (int): current iteration
            f (tf.Variable): current estimate of function values
            delta (tf.Variable): change in step size from previous iteration

        Returns: true if continue, false if converged

        """
        return tf.logical_and(tf.less(it, max_it), tf.greater(delta, 1e-5))


    def run(self, max_it):
        """
        Runs Kronecker inference
        Args:
            mu (tf.Variable): prior mean
            max_it (int): maximum number of iterations for Kronecker inference
            f (tf.Variable): uninitialized function values

        Returns:

        """
        delta = tfe.Variable(sys.float_info.max)
        prev = tfe.Variable(sys.float_info.max)
        it = tfe.Variable(0)

        out = tf.while_loop(self.conv, self.step, [max_it, it, prev, delta])
        self.f = kron_mvp(self.Ks, self.alpha) + self.mu
        self.grads = self.grad_func(self.y, self.f)[0]

        return out


    def cg_prod(self, p):

        if self.k_diag is None:
            return p + tf.multiply(tf.sqrt(self.W), kron_mvp(self.Ks, tf.multiply(tf.sqrt(self.W), p)))

        else:
            Wp = tf.multiply(tf.sqrt(self.W), p)
            return p + tf.multiply(tf.sqrt(self.W), kron_mvp(self.Ks, Wp) + tf.multiply(self.k_diag, Wp))

    def search_step(self, obj_prev, obj_search, min_obj, alpha, delta_alpha,
                    y, step_size, grad_norm, max_it, t, mu, opt_step):
        """
        Executes one step of a backtracking line search
        Args:
            obj_prev (tf.Variable): previous objective
            obj_search (tf.Variable): current objective
            min_obj (tf.Variable): current minimum objective
            alpha (tf.Variable): current search point
            delta_alpha (tf.Variable): change in step size from last iteration
            y (tf.Variable): realized function values from GP
            step_size (tf.Variable): current step size
            grad_norm (tf.Variable): norm of gradient
            max_it (int): maximum number of line search iterations
            t (tf.Variable): current line search iteration
            mu (tf.Variable): prior mean
            opt_step (tf.Variable): optimal step size until now

        Returns:

        """
        alpha_search = tf.squeeze(alpha + step_size * delta_alpha)

        f_search = tf.squeeze(kron_mvp(self.Ks, alpha_search)) + mu

        if self.k_diag is not None:
            f_search += tf.multiply(self.k_diag, alpha_search)

        if self.mask is not None:
            y_lim = tf.boolean_mask(self.y, self.mask)
            f_lim = tf.boolean_mask(f_search, self.mask)
            alpha_lim = tf.boolean_mask(alpha_search, self.mask)
            mu_lim = tf.boolean_mask(self.mu, self.mask)
            obj_search = -tf.reduce_sum(self.likelihood.log_like(y_lim, f_lim)) + \
                  0.5 * tf.reduce_sum(tf.multiply(alpha_lim, f_lim - mu_lim))

        else:
            obj_search = -tf.reduce_sum(self.likelihood.log_like(y, f_search)) + 0.5 * tf.reduce_sum(
                tf.multiply(alpha_search, f_search - mu))


        opt_step = tf.cond(tf.greater(min_obj, obj_search), lambda: step_size, lambda: opt_step)
        min_obj = tf.cond(tf.greater(min_obj, obj_search), lambda: obj_search, lambda: min_obj)

        step_size = self.tau * step_size
        t = t + 1

        return obj_prev, obj_search, min_obj, alpha, delta_alpha, y,\
               step_size, grad_norm, max_it, t, mu, opt_step


    def converge_cond(self, obj_prev, obj_search, min_obj, alpha,
                      delta_alpha, y, step_size, grad_norm, max_it, t, mu, opt_step):
        """

        Assesses convergence of line search. Same params as above.

        """

        return tf.logical_and(tf.less(t, max_it), tf.less(obj_prev - obj_search, step_size*t))



    def line_search(self, alpha, delta_alpha, y, obj_prev, max_it, mu):
        """
        Executes line search for optimal Newton step
        Args:
            alpha (tf.Variable): search direction
            delta_alpha (tf.Variable): change in search direction
            y (tf.Variable): realized values from GP point process
            obj_prev (tf.Variable): previous objective value
            max_it (int): maximum number of iterations
            mu (tf.Variable): prior mean

        Returns: (min objective, optimal step size)

        """
        obj_search = sys.float_info.max
        min_obj = obj_prev

        step_size = 5.0
        opt_step = 0.0

        grad_norm = tf.reduce_sum(tf.multiply(alpha, alpha))
        t = 1


        res = tf.while_loop(self.converge_cond, self.search_step, [obj_prev, obj_search, min_obj, alpha, delta_alpha,
                                                         y, step_size, grad_norm, max_it, t, mu, opt_step])

        return res[2], res[-1]


    def marginal(self, Ks_new = None):
        """
        calculates marginal likelihood
        Args:
            f (tf.Variable): function values
            mu (tf.Variable): prior mean
            self.W (tf.Variable): negative Hessian of likelihood

        Returns: tf.Variable for marginal likelihood

        """

        if Ks_new == None:
            Ks = self.Ks
        else:
            Ks = Ks_new

        eigs = [tf.expand_dims(tf.self_adjoint_eig(K)[0], 1) for K in Ks]
        eig_K = tf.squeeze(kron_list(eigs))

        if self.mask is not None:

            y_lim = tf.boolean_mask(self.y, self.mask)
            f_lim = tf.boolean_mask(self.f, self.mask)
            alpha_lim = tf.boolean_mask(self.alpha, self.mask)
            mu_lim = tf.boolean_mask(self.mu, self.mask)
            W_lim = tf.boolean_mask(self.W, self.mask)
            eig_k_lim = tf.boolean_mask(eig_K, self.mask)

            return -0.5 * tf.reduce_sum(tf.multiply(alpha_lim, f_lim - mu_lim)) - \
                   0.5 * tf.reduce_sum(tf.log(1 + tf.multiply(eig_k_lim, W_lim))) + \
                   tf.reduce_sum(self.likelihood.log_like(y_lim, f_lim))

        return -0.5 * tf.reduce_sum(tf.multiply(self.alpha, self.f - self.mu)) - \
               0.5*tf.reduce_sum(tf.log(1 + tf.multiply(eig_K, self.W))) +\
               tf.reduce_sum(self.likelihood.log_like(self.y, self.f))

    def variance(self, n_s):

        n = self.X.shape[0]

        for i in range(n_s):

            g_m = tf.contrib.distributions.MultivariateNormalDiag(tf.zeros(n), tf.eye(n))
            g_n = tf.contrib.distributions.MultivariateNormalDiag(tf.zeros(n), tf.eye(n))

            eig_K = kron_list([tf.matmul(v, tf.matmul(tf.sqrt(e), v)) for e,v in self.K_eigs])

            right_side = tf.matmul(eig_K, g_m) + g_n

            Ar = self.opt.cg(right_side, A = self.Ks)



    def predict_mean(self, x_new):

        k_dims = [self.kernel.eval(np.expand_dims(np.unique(self.X[:, d]), 1), np.expand_dims(x_new[:, d], 1))
                  for d in self.X.shape[1]]

        kx = tf.squeeze(kron_list(k_dims))

        mean = tf.reduce_sum(tf.multiply(kx, self.alpha)) + self.mu[0]

        return mean




class CGOptimizer:

    def __init__(self, cg_prod = None):

        self.cg_prod = cg_prod

    def cg_converged(self, p, count, x, r, max_it, precondition, z):
        """
        Assesses convergence of CG
        Args:
            A (tf.Variable): matrix on left side of linear system
            p (tf.Variable): search direction
            r_k_norm (tf.Variable): norm of r_k
            count (int): iteration number
            x (tf.Variable): current estimate of solution to linear system
            r (tf.Variable): current residual (b - Ax)
            n (int): size of b

        Returns: false if converged, true if not

        """
        return tf.logical_and(tf.greater(tf.reduce_sum(tf.multiply(r, r)), 1e-5), tf.less(count, max_it))

    def cg_body(self, p, count, x, r, max_it, precondition, z):
        """

        Executes one step of conjugate gradient descent

        Args:
            A (tf.Variable): matrix on left side of linear system
            p (tf.Variable): search direction
            r_k_norm (tf.Variable): norm of r_k
            count (int): iteration number
            x (tf.Variable): current estimate of solution to linear system
            r (tf.Variable): current residual (p - Ax)
            n (int): size of b

        Returns: updated parameters for CG
        """
        count = count + 1
        Bp = self.cg_prod(p)

        if precondition is not None:
            norm_k = tf.reduce_sum(tf.multiply(r, z))
        else:
            norm_k = tf.reduce_sum(tf.multiply(r, r))

        alpha = norm_k / tf.reduce_sum(tf.multiply(Bp, p))
        x += alpha * p
        r -= alpha * Bp

        if precondition is not None:
            z = tf.multiply(1.0/precondition, r)
            norm_next = tf.reduce_sum(tf.multiply(z, r))
        else:
            norm_next = tf.reduce_sum(tf.multiply(r, r))

        beta = norm_next / norm_k

        if precondition is not None:
            p = z + beta*p
        else:
            p = r + beta*p

        return p, count, x, r, max_it, precondition, z

    def cg(self, b, x=None, A= None, precondition=None, z=None):
        """
        solves linear system Ax = b
        Args:
            A (tf.Variable): matrix A
            b (tf.Variable): vector b
            x (): solution
            precondition(): diagonal of preconditioning matrix

        Returns: returns x that solves linear system

        """
        count = tf.constant(0)
        n = b.get_shape().as_list()[0]

        if not x:
            x = tf.zeros(shape=[n])

        r =  b - self.cg_prod(x)

        if precondition is not None:
            z = tf.multiply(1.0/precondition, r)
            p = z

        else:
            p = r

        fin = tf.while_loop(self.cg_converged, self.cg_body, [p, count, x,
                                                              r, 2 * n, precondition, z])

        return fin[2]



class KernelLearner:

    def __init__(self, mu, kernel, likelihood, X, y, tau,
                 k_diag = None, mask = None, eps = np.array([1e-5, 1])):

        self.kernel = kernel
        self.mu = mu
        self.likelihood = likelihood
        self.X = X
        self.y = y
        self.tau = tau
        self.k_diag = k_diag
        self.mask = mask
        self.eps = eps

    def optimize_marginal(self, init_params):

        return 0

    def gradient_step(self, params):

        for i in range(len(params)):

            fin_diff = self.finite_difference(self.eps[i], params, i)

        return 0

    def finite_difference(self, epsilon, params, i):

        param_step = deepcopy(params)

        param_step[i] += self.eps[i]
        marg_plus = self.get_marginal(param_step)

        param_step[i] -= 2 * self.eps[i]
        marg_minus = self.get_marginal(param_step)

        fin_diff = (marg_plus - marg_minus) / (2 * self.eps[i])

        return fin_diff

    def get_marginal(self, params):

        kernel = self.kernel(*params)
        solver = KroneckerSolver(self.mu, kernel, self.likelihood, self.X, self.y,
                                 self.tau, self.k_diag, self.mask, verbose=False)
        solver.run(10)
        marg = solver.marginal()
        return marg


def kron(A, B):
    """
    Kronecker product of two matrices
    Args:
        A (tf.Variable): first matrix for kronecker product
        B (tf.Variable): second matrix

    Returns: kronecker product of A and B

    """

    n_col = A.shape[1] * B.shape[1]
    out = tf.zeros([0, n_col])

    for i in range(A.shape[0]):

        row = tf.zeros([B.shape[0], 0])

        for j in range(A.shape[1]):
            row = tf.concat([row, A[i, j] * B], 1)

        out = tf.concat([out, row], 0)

    return out

def kron_list(matrices):
    """
    Kronecker product of a list of matrices
    Args:
        matrices (list of tf.Variable): list of matrices

    Returns:

    """
    out = kron(matrices[0], matrices[1])

    for i in range(2, len(matrices)):
        out = kron(out, matrices[i])

    return out

def kron_mvp(Ks, v):
    """
    Matrix vector product using Kronecker structure
    Args:
        Ks (list of tf.Variable): list of matrices corresponding to kronecker decomposition
        of K
        v (tf.Variable): vector to multiply K by

    Returns: matrix vector product of K and v

    """

    V_rows = Ks[0].shape[0]
    V_cols = v.shape[0] / V_rows
    V = tf.transpose(tf.reshape(v, (V_rows, V_cols)))
    mvp = V

    for k in reversed(Ks):
        mvp = tf.reshape(tf.transpose(tf.matmul(k, mvp)), [V_rows, V_cols])

    return tf.squeeze(tf.reshape(tf.transpose(mvp), [1, -1]))




