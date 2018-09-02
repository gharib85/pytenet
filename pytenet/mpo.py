import numpy as np


class MPO(object):
    """
    Matrix product operator (MPO) class.

    The i-th MPO tensor has dimension [d, d, D[i], D[i+1]] with d the physical dimension
    and D the list of virtual bond dimensions.
    """

    def __init__(self, d, **kwargs):
        """
        Args:
            d: physical dimension
        Keyword args: either provide oplist and L, or D and (optionally) fill
            oplist: list of operator chains
            L: number of lattice sites (only accessed if oplist is provided)
            D: virtual bond dimensions
            fill: numerical value for filling the MPO tensors, or
                 'random' for normally distributed random entries
        """
        self.d = d
        if 'opchains' in kwargs:
            if not 'L' in kwargs:
                raise ValueError('number of lattice sites L must be provided as keyword argument together with list of opchains')
            self.from_opchains(d, kwargs['L'], kwargs['opchains'])
        else:
            D    = kwargs.get('D',    [])
            fill = kwargs.get('fill', 0.0)
            if isinstance(fill, int) or isinstance(fill, float) or isinstance(fill, complex):
                self.A = [np.full((d, d, D[i], D[i+1]), fill) for i in range(len(D)-1)]
            elif fill == 'random':
                # random complex entries
                self.A = [
                        np.random.normal(size=(d, d, D[i], D[i+1]), scale=1./np.sqrt(d*D[i]*D[i+1])) +
                     1j*np.random.normal(size=(d, d, D[i], D[i+1]), scale=1./np.sqrt(d*D[i]*D[i+1])) for i in range(len(D)-1)]
            else:
                raise ValueError('fill = {} invalid; must be a number or "random"'.format(fill))

    @property
    def nsites(self):
        """Number of lattice sites."""
        return len(self.A)

    @property
    def bond_dims(self):
        """Virtual bond dimensions."""
        if len(self.A) == 0:
            return []
        else:
            D = [self.A[i].shape[2] for i in range(len(self.A))]
            D.append(self.A[-1].shape[3])
            return D

    def orthonormalize(self, mode='left'):
        """Left- or right-orthonormalize the MPO (Frobenius norm) using QR decompositions."""
        if len(self.A) == 0:
            return

        if mode == 'left':
            for i in range(len(self.A) - 1):
                self.A[i], self.A[i+1] = local_orthonormalize_left_qr(self.A[i], self.A[i+1])
            # last tensor
            self.A[-1], T = local_orthonormalize_left_qr(self.A[-1], np.array([[[[1]]]]))
            # normalization factor (real-valued since diagonal of R matrix is real)
            assert T.shape == (1, 1, 1, 1)
            nrm = T[0, 0, 0, 0].real
            if nrm < 0:
                # flip sign such that normalization factor is always non-negative
                self.A[-1] = -self.A[-1]
                nrm = -nrm
            return nrm
        elif mode == 'right':
            for i in reversed(range(1, len(self.A))):
                self.A[i], self.A[i-1] = local_orthonormalize_right_qr(self.A[i], self.A[i-1])
            # first tensor
            self.A[0], T = local_orthonormalize_right_qr(self.A[0], np.array([[[[1]]]]))
            # normalization factor (real-valued since diagonal of R matrix is real)
            assert T.shape == (1, 1, 1, 1)
            nrm = T[0, 0, 0, 0].real
            if nrm < 0:
                # flip sign such that normalization factor is always non-negative
                self.A[0] = -self.A[0]
                nrm = -nrm
            return nrm
        else:
            raise ValueError('mode = {} invalid; must be "left" or "right".'.format(mode))

    def as_matrix(self):
        """Merge all tensors to obtain the matrix representation on the full Hilbert space."""
        op = self.A[0]
        for i in range(1, len(self.A)):
            op = merge_MPO_tensor_pair(op, self.A[i])
        assert op.ndim == 4
        assert op.shape[2] == 1 and op.shape[3] == 1
        op = op.reshape((op.shape[0], op.shape[1]))
        return op

    def from_opchains(self, d, L, opchains):
        """Construct a MPO representation of a sum of "operator chains"."""

        # filter out empty operator chains
        opchains = [opc for opc in opchains if opc.length > 0]

        if len(opchains) == 0:
            # dummy zero tensors
            self.A = [np.zeros((d, d, 1, 1), dtype=complex) for _ in range(L)]
            return

        opchains = sorted(opchains, key=lambda o: o.iend*L + o.length)

        # right-pad first operator chain with identity matrices
        # (required for trailing identity operations in each chain)
        opchains[0].pad_identities_right(d, L)

        # find operator chain with largest starting index
        maxidxS = np.argmax([op.istart for op in opchains])
        # left-pad this operator chain with identity matrices (for leading identity operations in each chain)
        opchains[maxidxS].pad_identities_left(d)

        # allocate virtual bond slots between operators for each operator chain
        slotidx = [0] * (L+1)
        slotidx[ 0] = 1
        slotidx[-1] = 1
        opslots = [[]] * len(opchains)
        for j, opc in enumerate(opchains):
            opslots[j] = [0] * opc.length
            for i in range(opc.length-1):
                k = opc.istart + i + 1
                opslots[j][i] = slotidx[k]
                slotidx[k] += 1
            # last slot is 0 (for trailing identity matrices)

        # allocate and fill MPO tensors
        self.A = [np.zeros((slotidx[j], slotidx[j+1], d, d), dtype=complex) for j in range(L)]
        for j, opc in enumerate(opchains):
            for i in range(opc.length):
                if i==0:
                    if opc.istart == 0:
                        k = 0
                    else:
                        k = opslots[maxidxS][opc.istart-1]
                else:
                    k = opslots[j][i-1]
                # add to A (instead of simply assigning) to handle sum of single-site operators without dedicated bond slots
                self.A[opc.istart + i][k, opslots[j][i]] += opc.oplist[i]

        self.A = [W.transpose((2, 3, 0, 1)) for W in self.A]


def local_orthonormalize_left_qr(A, Anext):
    """
    Left-orthonormalize local site tensor A by a QR decomposition,
    and update tensor at next site.
    """
    # perform QR decomposition and replace A by reshaped Q matrix
    s = A.shape
    assert len(s) == 4
    Q, R = np.linalg.qr(A.reshape((s[0]*s[1]*s[2], s[3])), mode='reduced')
    A = Q.reshape((s[0], s[1], s[2], Q.shape[1]))
    # update Anext tensor: multiply with R from left
    Anext = np.tensordot(R, Anext, (1, 2)).transpose((1, 2, 0, 3))
    return (A, Anext)


def local_orthonormalize_right_qr(A, Aprev):
    """
    Right-orthonormalize local site tensor A by a QR decomposition,
    and update tensor at previous site.
    """
    # flip left and right virtual bond dimensions
    A = A.transpose((0, 1, 3, 2))
    # perform QR decomposition and replace A by reshaped Q matrix
    s = A.shape
    assert len(s) == 4
    Q, R = np.linalg.qr(A.reshape((s[0]*s[1]*s[2], s[3])), mode='reduced')
    A = Q.reshape((s[0], s[1], s[2], Q.shape[1])).transpose((0, 1, 3, 2))
    # update Aprev tensor: multiply with R from right
    Aprev = np.tensordot(Aprev, R, (3, 1))
    return (A, Aprev)


def merge_MPO_tensor_pair(A0, A1):
    """Merge two neighboring MPO tensors."""
    A = np.tensordot(A0, A1, (3, 2))
    # pair original physical dimensions of A0 and A1
    A = A.transpose((0, 3, 1, 4, 2, 5))
    # combine original physical dimensions
    A = A.reshape((A.shape[0]*A.shape[1], A.shape[2]*A.shape[3], A.shape[4], A.shape[5]))
    return A
