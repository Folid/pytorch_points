"""
code courtesy of
https://github.com/erikwijmans/Pointnet2_PyTorch
"""

import torch
import faiss
import numpy as np
from scipy import sparse

from .._ext import sampling, linalg
from ..utils.pytorch_utils import check_values, save_grad, saved_variables

if torch.cuda.is_available():
    from .faiss_setup import GPU_RES


def channel_shuffle(x, groups=2):
    '''Channel shuffle: [N,C,H,W] -> [N,g,C/g,H,W] -> [N,C/g,g,H,w] -> [N,C,H,W]'''
    N, C, H, W = x.size()
    g = groups
    return x.view(N, g, C/g, H, W).permute(0, 2, 1, 3, 4).reshape(N, C, H, W)


def jitter_perturbation_point_cloud(batch_data, sigma=0.005, clip=0.02, is_2D=False, NCHW=True):
    if NCHW:
        batch_data = batch_data.transpose(1, 2)

    batch_size = batch_data.shape[0]
    chn = 2 if is_2D else 3
    jittered_data = sigma * torch.randn_like(batch_data)
    for b in range(batch_size):
        jittered_data[b].clamp_(-clip[b].item(), clip[b].item())
    jittered_data[:, :, chn:] = 0
    jittered_data += batch_data
    if NCHW:
        jittered_data = jittered_data.transpose(1, 2)
    return jittered_data


def __swig_ptr_from_FloatTensor(x):
    assert x.is_contiguous()
    assert x.dtype == torch.float32
    return faiss.cast_integer_to_float_ptr(
        x.storage().data_ptr() + x.storage_offset() * 4)


def __swig_ptr_from_LongTensor(x):
    assert x.is_contiguous()
    assert x.dtype == torch.int64, 'dtype=%s' % x.dtype
    return faiss.cast_integer_to_long_ptr(
        x.storage().data_ptr() + x.storage_offset() * 8)


def search_index_pytorch(database, x, k):
    """
    KNN search via Faiss
    :param
        database BxNxC
        x BxMxC
    :return
        D BxMxK
        I BxMxK
    """
    Dptr = database.data_ptr()
    is_cuda = False
    if not (x.is_cuda or database.is_cuda):
        index = faiss.IndexFlatL2(database.size(-1))
    else:
        is_cuda = True
        index = faiss.GpuIndexFlatL2(GPU_RES, database.size(-1))  # dimension is 3
    index.add_c(database.size(0), faiss.cast_integer_to_float_ptr(Dptr))

    assert x.is_contiguous()
    n, d = x.size()
    assert d == index.d

    D = torch.empty((n, k), dtype=torch.float32, device=x.device)
    I = torch.empty((n, k), dtype=torch.int64, device=x.device)

    if is_cuda:
        torch.cuda.synchronize()
    xptr = __swig_ptr_from_FloatTensor(x)
    Iptr = __swig_ptr_from_LongTensor(I)
    Dptr = __swig_ptr_from_FloatTensor(D)
    index.search_c(n, xptr,
                   k, Dptr, Iptr)
    if is_cuda:
        torch.cuda.synchronize()
    index.reset()
    return D, I

class KNN(torch.autograd.Function):
    @staticmethod
    def forward(ctx, k, query, points):
        """
        :param k: k in KNN
               query: BxMxC
               points: BxNxC
        :return:
            neighbors_points: BxMxK
            index_batch: BxMxK
        """
        # selected_gt: BxkxCxM
        # process each batch independently.
        index_batch = []
        distance_batch = []
        for i in range(points.shape[0]):
            D_var, I_var = search_index_pytorch(points[i], query[i], k)
            GPU_RES.syncDefaultStreamCurrentDevice()
            index_batch.append(I_var)  # M, k
            distance_batch.append(D_var)  # M, k

        # B, M, K
        index_batch = torch.stack(index_batch, dim=0)
        distance_batch = torch.stack(distance_batch, dim=0)
        ctx.mark_non_differentiable(index_batch, distance_batch)
        return index_batch, distance_batch


def faiss_knn(k, query, points, NCHW=True):
    """
    group batch of points to neighborhoods
    :param
        k: neighborhood size
        query: BxCxM or BxMxC
        points: BxCxN or BxNxC
        NCHW: if true, the second dimension is the channel dimension
    :return
        neighbor_points BxCxMxk (if NCHW) or BxMxkxC (otherwise)
        index_batch     BxMxk
        distance_batch  BxMxk
    """
    if NCHW:
        batch_size, channels, num_points = points.size()
        points_trans = points.transpose(2, 1).contiguous()
        query_trans = query.transpose(2, 1).contiguous()
    else:
        points_trans = points.contiguous()
        query_trans = query.contiguous()

    batch_size, num_points, _ = points_trans.size()
    # BxMxk
    index_batch, distance_batch = KNN.apply(k, query_trans, points_trans)
    # BxNxC -> BxMxNxC
    points_expanded = points_trans.unsqueeze(dim=1).expand(
        (-1, query_trans.size(1), -1, -1))
    # BxMxk -> BxMxkxC
    index_batch_expanded = index_batch.unsqueeze(dim=-1).expand(
        (-1, -1, -1, points_trans.size(-1)))
    # BxMxkxC
    neighbor_points = torch.gather(points_expanded, 2, index_batch_expanded)
    index_batch = index_batch
    if NCHW:
        # BxCxMxk
        neighbor_points = neighbor_points.permute(0, 3, 1, 2).contiguous()
    return neighbor_points, index_batch, distance_batch


def __batch_distance_matrix_general(A, B):
    """
    :param
        A, B [B,N,C], [B,M,C]
    :return
        D [B,N,M]
    """
    r_A = torch.sum(A * A, dim=2, keepdim=True)
    r_B = torch.sum(B * B, dim=2, keepdim=True)
    m = torch.matmul(A, B.permute(0, 2, 1))
    D = r_A - 2 * m + r_B.permute(0, 2, 1)
    return D


def group_knn(k, query, points, unique=True, NCHW=True):
    """
    group batch of points to neighborhoods
    :param
        k: neighborhood size
        query: BxCxM or BxMxC
        points: BxCxN or BxNxC
        unique: neighborhood contains *unique* points
        NCHW: if true, the second dimension is the channel dimension
    :return
        neighbor_points BxCxMxk (if NCHW) or BxMxkxC (otherwise)
        index_batch     BxMxk
        distance_batch  BxMxk
    """
    if NCHW:
        batch_size, channels, num_points = points.size()
        points_trans = points.transpose(2, 1).contiguous()
        query_trans = query.transpose(2, 1).contiguous()
    else:
        points_trans = points.contiguous()
        query_trans = query.contiguous()

    batch_size, num_points, _ = points_trans.size()
    assert(num_points >= k
           ), "points size must be greater or equal to k"

    D = __batch_distance_matrix_general(query_trans, points_trans)
    if unique:
        # prepare duplicate entries
        points_np = points_trans.detach().cpu().numpy()
        indices_duplicated = np.ones(
            (batch_size, 1, num_points), dtype=np.int32)

        for idx in range(batch_size):
            _, indices = np.unique(points_np[idx], return_index=True, axis=0)
            indices_duplicated[idx, :, indices] = 0

        indices_duplicated = torch.from_numpy(
            indices_duplicated).to(device=D.device, dtype=torch.float32)
        D += torch.max(D) * indices_duplicated

    # (B,M,k)
    distances, point_indices = torch.topk(-D, k, dim=-1, sorted=True)
    # (B,N,C)->(B,M,N,C), (B,M,k)->(B,M,k,C)
    knn_trans = torch.gather(points_trans.unsqueeze(1).expand(-1, query_trans.size(1), -1, -1),
                             2,
                             point_indices.unsqueeze(-1).expand(-1, -1, -1, points_trans.size(-1)))

    if NCHW:
        knn_trans = knn_trans.permute(0, 3, 1, 2)

    return knn_trans, point_indices, -distances


class GatherFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, features, idx):
        r"""
        Parameters
        ----------
        features : torch.Tensor
            (B, C, N) tensor
        idx : torch.Tensor
            (B, npoint) tensor of the features to gather
        Returns
        -------
        torch.Tensor
            (B, C, npoint) tensor
        """
        features = features.contiguous()
        idx = idx.contiguous()
        idx = idx.to(dtype=torch.int32)

        B, npoint = idx.size()
        _, C, N = features.size()

        output = torch.empty(
            B, C, npoint, dtype=features.dtype, device=features.device)
        sampling.gather_forward(
            B, C, N, npoint, features, idx, output
        )

        ctx.save_for_backward(idx)
        ctx.C = C
        ctx.N = N
        return output

    @staticmethod
    def backward(ctx, grad_out):
        idx, = ctx.saved_tensors
        B, npoint = idx.size()

        grad_features = torch.zeros(
            B, ctx.C, ctx.N, dtype=grad_out.dtype, device=grad_out.device)
        sampling.gather_backward(
            B, ctx.C, ctx.N, npoint, grad_out.contiguous(), idx, grad_features
        )

        return grad_features, None


gather_points = GatherFunction.apply  # type: ignore


class BallQuery(torch.autograd.Function):
    @staticmethod
    def forward(ctx, radius, nsample, xyz, new_xyz):
        r"""
        Parameters
        ----------
        radius : float
            radius of the balls
        nsample : int
            maximum number of features in the balls
        xyz : torch.Tensor
            (B, N, 3) xyz coordinates of the features
        new_xyz : torch.Tensor
            (B, npoint, 3) centers of the ball query
        Returns
        -------
        torch.Tensor
            (B, npoint, nsample) tensor with the indicies of the features that form the query balls
        """
        return sampling.ball_query(new_xyz, xyz, radius, nsample)

    @staticmethod
    def backward(ctx, a=None):
        return None, None, None, None


ball_query = BallQuery.apply  # type: ignore


class GroupingOperation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, features, idx):
        r"""
        Parameters
        ----------
        features : torch.Tensor
            (B, C, N) tensor of features to group
        idx : torch.Tensor
            (B, npoint, nsample) tensor containing the indicies of features to group with
        Returns
        -------
        torch.Tensor
            (B, C, npoint, nsample) tensor
        """
        B, nfeatures, nsample = idx.size()
        _, C, N = features.size()

        ctx.for_backwards = (idx, N)

        return sampling.group_points(features, idx)

    @staticmethod
    def backward(ctx, grad_out):
        r"""
        Parameters
        ----------
        grad_out : torch.Tensor
            (B, C, npoint, nsample) tensor of the gradients of the output from forward
        Returns
        -------
        torch.Tensor
            (B, C, N) gradient of the features
        None
        """
        idx, N = ctx.for_backwards

        grad_features = sampling.group_points_grad(grad_out.contiguous(), idx, N)

        return grad_features, None


grouping_operation = GroupingOperation.apply  # type: ignore


class QueryAndGroup(torch.nn.Module):
    r"""
    Groups with a ball query of radius
    Parameters
    ---------
    radius : float32
        Radius of ball
    nsample : int32
        Maximum number of features to gather in the ball
    """

    def __init__(self, radius, nsample, use_xyz=True):
        super(QueryAndGroup, self).__init__()
        self.radius, self.nsample, self.use_xyz = radius, nsample, use_xyz

    def forward(self, xyz, new_xyz, features=None):
        r"""
        Parameters
        ----------
        xyz : torch.Tensor
            xyz coordinates of the features (B, N, 3)
        new_xyz : torch.Tensor
            centriods (B, npoint, 3)
        features : torch.Tensor
            Descriptors of the features (B, C, N)
        Returns
        -------
        new_features : torch.Tensor
            (B, 3 + C, npoint, nsample) tensor
        """
        # (B, npoint, k)
        idx = ball_query(self.radius, self.nsample, xyz, new_xyz)
        # (B, 3, N)
        xyz_trans = xyz.transpose(1, 2).contiguous()
        grouped_xyz = grouping_operation(xyz_trans, idx)  # (B, 3, npoint, nsample)
        grouped_xyz -= new_xyz.transpose(1, 2).unsqueeze(-1)

        if features is not None:
            grouped_features = grouping_operation(features, idx)
            if self.use_xyz:
                new_features = torch.cat(
                    [grouped_xyz, grouped_features], dim=1
                )  # (B, C + 3, npoint, nsample)
            else:
                new_features = grouped_features
        else:
            assert (
                self.use_xyz
            ), "Cannot have not features and not use xyz as a feature!"
            new_features = grouped_xyz

        return new_features

class BatchSVDFunction(torch.autograd.Function):
    """
    batched svd implemented by https://github.com/KinglittleQ/torch-batch-svd
    """
    @staticmethod
    def forward(ctx, x):
        ctx.device = x.device
        if not torch.cuda.is_available():
            assert(RuntimeError), "BatchSVDFunction only runs on gpu"
        x = x.cuda()
        U, S, V = linalg.batch_svd_forward(x, True, 1e-7, 100)
        k = S.size(1)
        U = U[:, :, :k]
        V = V[:, :, :k]
        ctx.save_for_backward(x, U, S, V)
        U = U.to(ctx.device)
        S = S.to(ctx.device)
        V = V.to(ctx.device)
        return U, S, V

    @staticmethod
    def backward(ctx, grad_u, grad_s, grad_v):
        x, U, S, V = ctx.saved_variables

        grad_out = linalg.batch_svd_backward(
            [grad_u, grad_s, grad_v],
            x, True, True, U, S, V
        )

        return grad_out.to(device=ctx.device)


def batch_svd(x):
    """
    input:
        x --- shape of [B, M, N], k = min(M,N)
    return:
        U, S, V = batch_svd(x) where x = USV^T
        U [M, k]
        V [N, k]
        S [B, k] in decending order
    """
    assert(x.dim() == 3)
    return BatchSVDFunction.apply(x)

def normalize(tensor, dim=-1):
    """normalize tensor in specified dimension"""
    return torch.nn.functional.normalize(tensor, p=2, dim=dim, eps=1e-12, out=None)

def sqrNorm(tensor, dim=-1, keepdim=False):
    """squared L2 norm"""
    return torch.sum(tensor*tensor, dim=dim, keepdim=keepdim)


def dot_product(tensor1, tensor2, dim=-1, keepdim=False):
    return torch.sum(tensor1*tensor2, dim=dim, keepdim=keepdim)

def cross_product_2D(tensor1, tensor2, dim=1):
    assert(tensor1.shape[dim] == tensor2.shape[dim] and tensor1.shape[dim] == 2)
    output = torch.narrow(tensor1, dim, 0, 1) * torch.narrow(tensor2, dim, 1, 1) - torch.narrow(tensor1, dim, 1, 1) * torch.narrow(tensor2, dim, 0, 1)
    return output.squeeze(dim)

class ScatterAdd(torch.autograd.Function):
    @staticmethod
    def forward(ctx, src, idx, dim, out_size, fill=0.0):
        out = torch.full(out_size, fill, device=src.device, dtype=src.dtype)
        ctx.save_for_backward(idx)
        out.scatter_add_(dim, idx, src)
        ctx.mark_non_differentiable(idx)
        ctx.dim = dim
        return out

    @staticmethod
    def backward(ctx, ograd):
        idx, = ctx.saved_tensors
        grad = torch.gather(ograd, ctx.dim, idx)
        return grad, None, None, None, None

_scatter_add = ScatterAdd.apply

def scatter_add(src, idx, dim, out_size=None, fill=0.0):
    if out_size is None:
        out_size = list(src.size())
        dim_size = idx.max().item()+1
        out_size[dim] = dim_size
    return _scatter_add(src, idx, dim, out_size, fill)



if __name__ == '__main__':
    # from ..utils import pc_utils
    # cuda0 = torch.device('cuda:0')
    # pc = pc_utils.read_ply("/home/ywang/Documents/points/point-upsampling/3PU/prepare_data/polygonmesh_base/build/data_PPU_output/training/112/angel4_aligned_2.ply")
    # pc = pc[:, :3]
    # print("{} input points".format(pc.shape[0]))
    # pc_utils.save_ply(pc, "./input.ply", colors=None, normals=None)
    # pc = torch.from_numpy(pc).requires_grad_().to(cuda0).unsqueeze(0)
    # pc = pc.transpose(2, 1)

    # # test furthest point
    # idx, sampled_pc = furthest_point_sample(pc, 1250)
    # output = sampled_pc.transpose(2, 1).cpu().squeeze()
    # pc_utils.save_ply(output.detach(), "./output.ply", colors=None, normals=None)

    # # test KNN
    # knn_points, _, _ = group_knn(10, sampled_pc, pc, NCHW=True)  # B, C, M, K
    # labels = torch.arange(0, knn_points.size(2)).unsqueeze_(
    #     0).unsqueeze_(0).unsqueeze_(-1)  # 1, 1, M, 1
    # labels = labels.expand(knn_points.size(0), -1, -1,
    #                        knn_points.size(3))  # B, 1, M, K
    # # B, C, P
    # labels = torch.cat(torch.unbind(labels, dim=-1), dim=-1).squeeze().detach().cpu().numpy()
    # knn_points = torch.cat(torch.unbind(knn_points, dim=-1),
    #                        dim=-1).transpose(2, 1).squeeze(0).detach().cpu().numpy()
    # pc_utils.save_ply_property(knn_points, labels, "./knn_output.ply", cmap_name='jet')

    # from torch.autograd import gradcheck
    # # test = gradcheck(furthest_point_sample, [pc, 1250], eps=1e-6, atol=1e-4)
    # # print(test)
    # test = gradcheck(gather_points, [pc.to(  # type: ignore
    #     dtype=torch.float64), idx], eps=1e-6, atol=1e-4)

    # print(test)
    pass
