# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/MoCo-scene_and_view.ipynb (unless otherwise specified).

__all__ = ['MoCo_scene_and_view']

# Cell

import torch
import torch.nn as nn
from random import sample
import torch.nn as nn
import torch

import ipdb

from .encoder import Encoder
from .utils import pair_embeddings, stack_features_across_batch, convert_indices

# Cell
class MoCo_scene_and_view(nn.Module):
    """
    Build a MoCo model with: a query encoder, a key encoder, and a queue
    https://arxiv.org/abs/1911.05722
    """
    def __init__(self, dim=256, scene_r=35, view_r = 40, m=0.999, T=0.1, mlp=False, mode=None):
        """
        dim: feature dimension (default: 128)
        r: queue size; number of negative samples/prototypes (default: 16384)
        m: momentum for updating key encoder (default: 0.999)
        T: softmax temperature
        mlp: whether to use mlp projection
        """
        super(MoCo_scene_and_view, self).__init__()

        self.scene_r = scene_r
        self.view_r = view_r
        self.m = m
        self.T = T
        self.mode = mode
        self.dim=dim

        self.encoder_q = Encoder(dim = self.dim, mode=self.mode)
        self.encoder_k = Encoder(dim = self.dim, mode=self.mode)

        self.spatial_viewpoint_transformation = nn.Sequential(nn.Linear(263,256),
                                                              nn.ReLU(),
                                                              nn.Linear(256,256),
                                                              nn.ReLU(),
                                                              nn.Linear(256,self.dim))

        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)  # initialize
            param_k.requires_grad = False  # not update by gradient

        # create the scene queue
        self.register_buffer("queue_scene", torch.randn(dim, scene_r))
        self.queue_scene = nn.functional.normalize(self.queue_scene, dim=0)
        self.register_buffer("queue_scene_ptr", torch.zeros(1, dtype=torch.long))

        # create the view queue
        self.register_buffer("queue_view", torch.randn(dim, view_r))
        self.queue_view = nn.functional.normalize(self.queue_view, dim=0)
        self.register_buffer("queue_view_ptr", torch.zeros(1, dtype=torch.long))

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

    @torch.no_grad()
    def _dequeue_and_enqueue_scene(self, keys):

        batch_size = keys.shape[0]

        ptr = int(self.queue_scene_ptr)
        self.queue_scene[:, ptr:ptr + batch_size] = keys.T
        ptr = (ptr + batch_size) % self.scene_r  # move pointer

        if self.scene_r % batch_size != 0:
            ptr=0

        self.queue_scene_ptr[0] = ptr

    def _dequeue_and_enqueue_view(self, keys):

        batch_size = keys.shape[0]

        ptr = int(self.queue_view_ptr)

        # replace the keys at ptr (dequeue and enqueue)
        self.queue_view[:, ptr:ptr + batch_size] = keys.T
        ptr = (ptr + batch_size) % self.view_r  # move pointer

        self.queue_view_ptr[0] = ptr

    def merge_pose_with_scene_embeddings(self,
                                     scene_embeddings,
                                     view=None):
        '''
        Input
            scene_embeddings: output of scene_graph module. A list of of tensors containing node and
                              spatial embeddings of each batch element
            view : a tensor of size [batch, 1, 7] containing information of relative egomotion
                   between the two camera viewpoints
            transform_node and transform spatial: boolean flags whether to do any transformation on nodes or not
        Output
            scene_embeddings: concatenated with pose vectors
        '''

        merged_pose_embeds = []

        for batch_ind,(_, spatial_embeddings) in enumerate(scene_embeddings):
            num_obj_x = spatial_embeddings.shape[0]
            num_obj_y = spatial_embeddings.shape[1]

            # Broadcast view to spatial embedding dimension
            view_spatial = view[batch_ind].unsqueeze(0).repeat(num_obj_x, num_obj_y, 1)
            # Concatenate with visual embeddings
            pose_with_features = torch.cat((view_spatial,spatial_embeddings), dim=2)
            # Reassign the scene embeddings
#             scene_embeddings[batch_ind][1] = pose_with_features
            merged_pose_embeds.append([None,pose_with_features])
            ### To Do : Write some assertion test : (Saksham)

        return merged_pose_embeds


    def forward(self, feed_dict_q, feed_dict_k=None, metadata=None,
                      is_eval=False, cluster_result=None, index=None,
                      is_viewpoint_eval=False, feed_dicts_N=None, forward_type=None):
        """
        Input:
            feed_dict_q: a batch of query images and bounding boxes
            feed_dict_k: a batch of key images and bounding boxes
            is_eval: return momentum embeddings (used for clustering)
            cluster_result: cluster assignments, centroids, and density
            index: indices for training samples
        Output:
            logits, targets, proto_logits, proto_targets
        """

        mode = self.mode
        hyp_N = feed_dict_q["objects"][0].item()

        rel_viewpoint = metadata["rel_viewpoint"]

        if mode=="node":
            rel_viewpoint=None

        if is_viewpoint_eval and mode=="spatial":
            with torch.no_grad():
                k = self.encoder_q(feed_dict_q)
                k = self.merge_pose_with_scene_embeddings(k,rel_viewpoint) #merge
                for batch_ind in range(len(k)):
                    k[batch_ind][1] = self.spatial_viewpoint_transformation(k[batch_ind][1]) # Do viewpoint transformation on spatial embeddings
                k = stack_features_across_batch(k, mode)
                k = nn.functional.normalize(k, dim=1)
            return k

        if is_eval:
            with torch.no_grad():
                # the output from encoder is a list of features from the batch where each batch element (image)
                # might contain different number of objects
                k = self.encoder_q(feed_dict_q)

                # encoder output features in the list are stacked to form a tensor of features across the batch
                k = stack_features_across_batch(k, mode)

                # normalize feature across the batch
                k = nn.functional.normalize(k, dim=1)
            return k


        # k_o : spatial embeddings before viewpoint transformation
        # k_t : spatial embeds after viewpoint transformation


        # update the key encoder
        k_o = self.encoder_q(feed_dict_k) # callculate the embeddings

        # Do viewpoint transformation on embeddings if the pose is fed as input
        if mode=="spatial" and rel_viewpoint is not None:
            k_t = self.merge_pose_with_scene_embeddings(k_o, rel_viewpoint) #merge pose with spatial embeddings
            for batch_ind in range(len(k_t)):
                k_t[batch_ind][1] = self.spatial_viewpoint_transformation(k_t[batch_ind][1]) # Do viewpoint transformation on spatial embeddings

        k_o = stack_features_across_batch(k_o, mode)
        k_o = nn.functional.normalize(k_o, dim=1)

        k_t = stack_features_across_batch(k_t, mode)
        k_t = nn.functional.normalize(k_t, dim=1)

        q = self.encoder_q(feed_dict_q)  # queries: NxC
        q = stack_features_across_batch(q, mode)
        q = nn.functional.normalize(q, dim=1)

        if forward_type=="scene":
            # compute logits
            # Einstein sum is more intuitive
            # positive logits: Nx1
            l_pos = torch.einsum('nc,nc->n', [q, k_t]).unsqueeze(-1)
            # negative logits: Nxr
            l_neg = torch.einsum('nc,ck->nk', [q, self.queue_scene.clone().detach()])
            # logits: Nx(1+r)
            logits = torch.cat([l_pos, l_neg], dim=1)
            # apply temperature
            logits /= self.T
            # labels: positive key indicators
            labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

            # dequeue and enqueue
            self._dequeue_and_enqueue_scene(k_t)

            return logits, labels, None, None

            index = convert_indices(index,hyp_N, mode)

            # prototypical contrast
            if cluster_result is not None:
                proto_labels = []
                proto_logits = []
                for n, (im2cluster,prototypes,density) in enumerate(zip(cluster_result['im2cluster'],cluster_result['centroids'],cluster_result['density'])):
                    # get positive prototypes
                    pos_proto_id = im2cluster[index]
                    pos_prototypes = prototypes[pos_proto_id]

                    # sample negative prototypes
                    all_proto_id = [i for i in range(im2cluster.max())]

                    #print(len(pos_prototypes), len(all_proto_id))
                    neg_proto_id = set(all_proto_id)-set(pos_proto_id.tolist())
                    neg_proto_id = sample(neg_proto_id,self.scene_r) #sample r negative prototypes
                    neg_prototypes = prototypes[neg_proto_id]

                    proto_selected = torch.cat([pos_prototypes,neg_prototypes],dim=0)

                    # compute prototypical logits
                    logits_proto = torch.mm(q,proto_selected.t())

                    # targets for prototype assignment
                    labels_proto = torch.linspace(0, q.size(0)-1, steps=q.size(0)).long().cuda()

                    # scaling temperatures for the selected prototypes
                    temp_proto = density[torch.cat([pos_proto_id,torch.LongTensor(neg_proto_id).cuda()],dim=0)]
                    logits_proto /= temp_proto

                    proto_labels.append(labels_proto)
                    proto_logits.append(logits_proto)
                return logits, labels, proto_logits, proto_labels

        elif forward_type=="view":

            self.queue_view = torch.randn(self.dim, self.view_r).cuda()
            self.queue_view_ptr[0] = 0

            self._dequeue_and_enqueue_view(k_o)

            negative_view_index = [feed_n[1] for feed_n in feed_dicts_N]

            # getting encoding for scene_negatives
            for feed_dict_ in feed_dicts_N:
#                 with torch.no_grad():
                k_n = self.encoder_q(feed_dict_[0])
                # encoder output features in the list are stacked to form a tensor of features across the batch
                k_n = stack_features_across_batch(k_n, mode)
                # normalize feature across the batch
                scene_negatives = nn.functional.normalize(k_n, dim=1)
                # append negagives to queue_view
                self._dequeue_and_enqueue_view(scene_negatives)


            # positive logits: Nx1
            l_pos = torch.einsum('nc,nc->n', [q, k_t]).unsqueeze(-1)
            # negative logits: Nxr
            l_neg = torch.einsum('nc,ck->nk', [q, self.queue_view])
            # logits: Nx(1+r)
            logits = torch.cat([l_pos, l_neg], dim=1)
            # apply temperature
            logits /= self.T
            # labels: positive key indicators
            labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

            return logits, labels, None, None

        else:
            raise ValueError("Forward type of the mode must be defined")