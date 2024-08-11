# ------------------------------------------------------------------------
# IDOL: In Defense of Online Models for Video Instance Segmentation
# Copyright (c) 2022 ByteDance. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from SeqFormer (https://github.com/wjf5203/SeqFormer)
# Copyright (c) 2021 ByteDance. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from Deformable DETR (https://github.com/fundamentalvision/Deformable-DETR)
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Deformable DETR model and criterion classes.
"""
import torch
import torch.nn.functional as F
from torch import nn
import math

from ..util import box_ops
from ..util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)

from .backbone import build_backbone
from .matcher import build_matcher

# from .segmentation_condInst import (CondInst_segm,
#                            dice_loss, sigmoid_focal_loss)
####################################################################################
from .segmentation_condInst import (dice_loss, sigmoid_focal_loss,compute_project_term,compute_pairwise_term)
####################################################################################
from .deformable_transformer import build_deforamble_transformer
import copy
from fvcore.nn import giou_loss, smooth_l1_loss

def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])

############################################################################################################
def compute_locations(h, w, stride, device):
    shifts_x = torch.arange(
        0, w * stride, step=stride,
        dtype=torch.float32, device=device
    )
    shifts_y = torch.arange(
        0, h * stride, step=stride,
        dtype=torch.float32, device=device
    )
    shift_y, shift_x = torch.meshgrid(shifts_y, shifts_x)
    shift_x = shift_x.reshape(-1)
    shift_y = shift_y.reshape(-1)
    locations = torch.stack((shift_x, shift_y), dim=1) + stride // 2
    return locations
def parse_dynamic_params(params, channels, weight_nums, bias_nums):
    assert params.dim() == 2
    assert len(weight_nums) == len(bias_nums)
    assert params.size(1) == sum(weight_nums) + sum(bias_nums)

    num_insts = params.size(0)
    num_layers = len(weight_nums)

    params_splits = list(torch.split_with_sizes(
        params, weight_nums + bias_nums, dim=1
    ))

    weight_splits = params_splits[:num_layers]
    bias_splits = params_splits[num_layers:]

    for l in range(num_layers):
        if l < num_layers - 1:
            # out_channels x in_channels x 1 x 1
            weight_splits[l] = weight_splits[l].reshape(num_insts * channels, -1, 1, 1)
            bias_splits[l] = bias_splits[l].reshape(num_insts * channels)
        else:
            # out_channels x in_channels x 1 x 1
            weight_splits[l] = weight_splits[l].reshape(num_insts * 1, -1, 1, 1)
            bias_splits[l] = bias_splits[l].reshape(num_insts)

    return weight_splits, bias_splits


def aligned_bilinear(tensor, factor):
    assert tensor.dim() == 4
    assert factor >= 1
    assert int(factor) == factor

    if factor == 1:
        return tensor

    h, w = tensor.size()[2:]
    tensor = F.pad(tensor, pad=(0, 1, 0, 1), mode="replicate")
    oh = factor * h + 1
    ow = factor * w + 1
    tensor = F.interpolate(
        tensor, size=(oh, ow),
        mode='bilinear',
        align_corners=True
    )
    tensor = F.pad(
        tensor, pad=(factor // 2, 0, factor // 2, 0),
        mode="replicate"
    )

    return tensor[:, :, :oh - 1, :ow - 1]

############################################################################################################

class DeformableDETR(nn.Module):
    """ This is the Deformable DETR module that performs object detection """
    def __init__(self, backbone, transformer, num_classes, num_frames, num_queries, num_feature_levels,
                 aux_loss=True, with_box_refine=False, two_stage=False):
        """ Initializes the model.
        Parameters:
            backbone: torch module of the backbone to be used. See backbone.py
            transformer: torch module of the transformer architecture. See transformer.py
            num_classes: number of object classes
            num_queries: number of object queries, ie detection slot. This is the maximal number of objects
                         DETR can detect in a single image. For COCO, we recommend 100 queries.
            aux_loss: True if auxiliary decoding losses (loss at each decoder layer) are to be used.
            with_box_refine: iterative bounding box refinement
            two_stage: two-stage Deformable DETR
        """
        super().__init__()
        self.num_frames = num_frames
        self.num_queries = num_queries
        self.transformer = transformer
        self.num_classes = num_classes
        hidden_dim = transformer.d_model
        self.class_embed = nn.Linear(hidden_dim, num_classes)
        self.bbox_embed = MLP(hidden_dim, hidden_dim, 4, 3)
        self.num_feature_levels = num_feature_levels
        
        self.query_embed = nn.Embedding(num_queries, hidden_dim*2)
        if num_feature_levels > 1:
            num_backbone_outs = len(backbone.strides)
            input_proj_list = []
            for _ in range(num_backbone_outs):
                in_channels = backbone.num_channels[_]
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
            for _ in range(num_feature_levels - num_backbone_outs):
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=3, stride=2, padding=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
                in_channels = hidden_dim
            self.input_proj = nn.ModuleList(input_proj_list)
        else:
            self.input_proj = nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(backbone.num_channels[0], hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                )])
        self.backbone = backbone
        self.aux_loss = aux_loss
        self.with_box_refine = with_box_refine
        self.two_stage = two_stage

        prior_prob = 0.01
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        self.class_embed.bias.data = torch.ones(num_classes) * bias_value
        nn.init.constant_(self.bbox_embed.layers[-1].weight.data, 0)
        nn.init.constant_(self.bbox_embed.layers[-1].bias.data, 0)
        for proj in self.input_proj:
            nn.init.xavier_uniform_(proj[0].weight, gain=1)
            nn.init.constant_(proj[0].bias, 0)

        # if two-stage, the last class_embed and bbox_embed is for region proposal generation
        num_pred = (transformer.decoder.num_layers + 1) if two_stage else transformer.decoder.num_layers
        if with_box_refine:
            self.class_embed = _get_clones(self.class_embed, num_pred)
            self.bbox_embed = _get_clones(self.bbox_embed, num_pred)
            nn.init.constant_(self.bbox_embed[0].layers[-1].bias.data[2:], -2.0)
            # hack implementation for iterative bounding box refinement
            self.transformer.decoder.bbox_embed = self.bbox_embed
        else:
            nn.init.constant_(self.bbox_embed.layers[-1].bias.data[2:], -2.0)
            self.class_embed = nn.ModuleList([self.class_embed for _ in range(num_pred)])
            self.bbox_embed = nn.ModuleList([self.bbox_embed for _ in range(num_pred)])
            self.transformer.decoder.bbox_embed = None
        if two_stage:
            # hack implementation for two-stage
            self.transformer.decoder.class_embed = self.class_embed
            for box_embed in self.bbox_embed:
                nn.init.constant_(box_embed.layers[-1].bias.data[2:], 0.0)


    def forward(self, samples: NestedTensor):
        """ The forward expects a NestedTensor, which consists of:
               - samples.tensor: batched images, of shape [num_frames x 3 x H x W]
               - samples.mask: a binary mask of shape [num_frames x H x W], containing 1 on padded pixels

            It returns a dict with the following elements:
               - "pred_logits": the classification logits (including no-object) for all queries.
                                Shape= [batch_size x num_queries x (num_classes + 1)]
               - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                               (center_x, center_y, height, width). These values are normalized in [0, 1],
                               relative to the size of each individual image (disregarding possible padding).
                               See PostProcess for information on how to retrieve the unnormalized bounding box.
               - "aux_outputs": Optional, only returned when auxilary losses are activated. It is a list of
                                dictionnaries containing the two above keys for each decoder layer.
        """
   
        if not isinstance(samples, NestedTensor):
            samples = nested_tensor_from_tensor_list(samples)
        
        features, pos = self.backbone(samples)
        srcs = []
        masks = []
        poses = []
        for l, feat in enumerate(features[1:]):
            # src: [nf*N, _C, Hi, Wi],
            # mask: [nf*N, Hi, Wi],
            # pos: [nf*N, C, H_p, W_p]
            src, mask = feat.decompose() 
            src_proj_l = self.input_proj[l](src)    # src_proj_l: [nf*N, C, Hi, Wi]
            
            # src_proj_l -> [nf, N, C, Hi, Wi]
            n,c,h,w = src_proj_l.shape
            src_proj_l = src_proj_l.reshape(n//self.num_frames, self.num_frames, c, h, w).permute(1,0,2,3,4)
            
            # mask -> [nf, N, Hi, Wi]
            mask = mask.reshape(n//self.num_frames, self.num_frames, h, w).permute(1,0,2,3)
            
            # pos -> [nf, N, Hi, Wi]
            np, cp, hp, wp = pos[l+1].shape
            pos_l = pos[l+1].reshape(np//self.num_frames, self.num_frames, cp, hp, wp).permute(1,0,2,3,4)
            for n_f in range(self.num_frames):
                srcs.append(src_proj_l[n_f])
                masks.append(mask[n_f])
                poses.append(pos_l[n_f])
                assert mask is not None

        if self.num_feature_levels > (len(features) - 1):
            _len_srcs = len(features) - 1
            for l in range(_len_srcs, self.num_feature_levels):
                if l == _len_srcs:
                    src = self.input_proj[l](features[-1].tensors)
                else:
                    src = self.input_proj[l](srcs[-1])
                m = samples.mask    # [nf*N, H, W]
                mask = F.interpolate(m[None].float(), size=src.shape[-2:]).to(torch.bool)[0]
                pos_l = self.backbone[1](NestedTensor(src, mask)).to(src.dtype)
                
                # src -> [nf, N, C, H, W]
                n, c, h, w = src.shape
                src = src.reshape(n//self.num_frames, self.num_frames, c, h, w).permute(1,0,2,3,4)
                mask = mask.reshape(n//self.num_frames, self.num_frames, h, w).permute(1,0,2,3)
                np, cp, hp, wp = pos_l.shape
                pos_l = pos_l.reshape(np//self.num_frames, self.num_frames, cp, hp, wp).permute(1,0,2,3,4)

                for n_f in range(self.num_frames):
                    srcs.append(src[n_f])
                    masks.append(mask[n_f])
                    poses.append(pos_l[n_f])

        query_embeds = None
        if not self.two_stage:
            query_embeds = self.query_embed.weight
        hs, memory, init_reference, inter_references, enc_outputs_class, enc_outputs_coord_unact = self.transformer(srcs, masks, poses, query_embeds)

        outputs_classes = []
        outputs_coords = []
        for lvl in range(hs.shape[0]):
            if lvl == 0:
                reference = init_reference
            else:
                reference = inter_references[lvl - 1]
            reference = inverse_sigmoid(reference)
            outputs_class = self.class_embed[lvl](hs[lvl])
            tmp = self.bbox_embed[lvl](hs[lvl])
            if reference.shape[-1] == 4:
                tmp += reference
            else:
                assert reference.shape[-1] == 2
                tmp[..., :2] += reference
            outputs_coord = tmp.sigmoid()
            outputs_classes.append(outputs_class)
            outputs_coords.append(outputs_coord)
        outputs_class = torch.stack(outputs_classes)
        outputs_coord = torch.stack(outputs_coords)

        out = {'pred_logits': outputs_class[-1], 'pred_boxes': outputs_coord[-1]}
        if self.aux_loss:
            out['aux_outputs'] = self._set_aux_loss(outputs_class, outputs_coord)

        if self.two_stage:
            enc_outputs_coord = enc_outputs_coord_unact.sigmoid()
            out['enc_outputs'] = {'pred_logits': enc_outputs_class, 'pred_boxes': enc_outputs_coord}
        return out

    @torch.jit.unused
    def _set_aux_loss(self, outputs_class, outputs_coord):
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b}
                for a, b in zip(outputs_class[:-1], outputs_coord[:-1])]


class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, num_classes, matcher, weight_dict, losses, focal_alpha=0.25, mask_out_stride=4, num_frames=1):
        """ Create the criterion.
        Parameters:
            num_classes: number of object categories, omitting the special no-object category
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            losses: list of all the losses to be applied. See get_loss for list of available losses.
            focal_alpha: alpha in Focal Loss
        """
        super().__init__()
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.losses = losses
        self.focal_alpha = focal_alpha
        self.mask_out_stride = mask_out_stride
        self.num_frames = num_frames
        ########################################################################################
        self.register_buffer("_iter", torch.zeros([1]))
        self.pairwise_color_thresh=0.3
        self._warmup_iters=10000
        # self.instance_of_fcos = None

        self.pairwise_dilation=2
        self.pairwise_size = 3
        ########################################################################################

    def loss_labels(self, outputs,  targets, ref_target, indices, num_boxes, log=True):
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        
        src_logits = outputs['pred_logits']
        batch_size = len(targets)
        target_classes = torch.full(src_logits.shape[:2], self.num_classes,
                                    dtype=torch.int64, device=src_logits.device)
        src_logits_list = []
        target_classes_o_list = []
        for batch_idx in range(batch_size):
            valid_query = indices[batch_idx][0]
            gt_multi_idx = indices[batch_idx][1]
            if len(gt_multi_idx)==0:
                continue      
            bz_src_logits = src_logits[batch_idx]    
            target_classes_o = targets[batch_idx]["labels"]
            target_classes[batch_idx,valid_query] =  target_classes_o[gt_multi_idx]
            

            src_logits_list.append(bz_src_logits[valid_query])
            target_classes_o_list.append(target_classes_o[gt_multi_idx])
            
        
        num_boxes = torch.cat(target_classes_o_list).shape[0] if len(target_classes_o_list) != 0 else 1

        target_classes_onehot = torch.zeros([src_logits.shape[0], src_logits.shape[1], src_logits.shape[2] + 1],
                                            dtype=src_logits.dtype, layout=src_logits.layout, device=src_logits.device)
        target_classes_onehot.scatter_(2, target_classes.unsqueeze(-1), 1)

        target_classes_onehot = target_classes_onehot[:,:,:-1]
        loss_ce = sigmoid_focal_loss(src_logits, target_classes_onehot, num_boxes, alpha=self.focal_alpha, gamma=2) * src_logits.shape[1]
        losses = {'loss_ce': loss_ce}

        return losses

    @torch.no_grad()
    def loss_cardinality(self, outputs,  targets, ref_target, indices, num_boxes):
        """ Compute the cardinality error, ie the absolute error in the number of predicted non-empty boxes
        This is not really a loss, it is intended for logging purposes only. It doesn't propagate gradients
        """
        pred_logits = outputs['pred_logits']
        device = pred_logits.device
        
        tgt_lengths = torch.as_tensor([len(v["labels"]) for v in targets], device=device)
        # Count the number of predictions that are NOT "no-object" (which is the last class)
        card_pred = (pred_logits.argmax(-1) != pred_logits.shape[-1] - 1).sum(1)
        card_err = F.l1_loss(card_pred.float(), tgt_lengths.float())
        losses = {'cardinality_error': card_err}
        return losses

    def loss_boxes(self, outputs,  targets, ref_target, indices, num_boxes):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, h, w), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        # idx = self._get_src_permutation_idx(indices)

        src_boxes = outputs['pred_boxes']

        batch_size = len(targets)
        pred_box_list = []
        tgt_box_list = []
        for batch_idx in range(batch_size):
            valid_query = indices[batch_idx][0]
            gt_multi_idx = indices[batch_idx][1]
            if len(gt_multi_idx)==0:
                continue 
            bz_src_boxes = src_boxes[batch_idx]    
            bz_target_boxes = targets[batch_idx]["boxes"]
            pred_box_list.append(bz_src_boxes[valid_query])
            tgt_box_list.append(bz_target_boxes[gt_multi_idx])

        if len(pred_box_list) != 0:
            src_boxes = torch.cat(pred_box_list)
            target_boxes = torch.cat(tgt_box_list)
            num_boxes = src_boxes.shape[0]
            
            loss_bbox = F.l1_loss(src_boxes, target_boxes, reduction='none')
            losses = {}
            losses['loss_bbox'] = loss_bbox.sum() / num_boxes
            loss_giou = giou_loss(box_ops.box_cxcywh_to_xyxy(src_boxes),box_ops.box_cxcywh_to_xyxy(target_boxes))
            losses['loss_giou'] = loss_giou.sum() / num_boxes
        else:
            losses = {'loss_bbox':outputs['pred_boxes'].sum()*0,
            'loss_giou':outputs['pred_boxes'].sum()*0}
        return losses

    def loss_masks(self, outputs,  targets, ref_target, indices, num_boxes):
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        # tgt_idx = self._get_tgt_permutation_idx(indices)

        src_masks = outputs["pred_masks"]
        if type(src_masks) == list:
            src_masks = torch.cat(src_masks, dim=1)[0]
        key_frame_masks = [t["masks"] for t in targets]
        ref_frame_masks = [t["masks"] for t in ref_target] 
        #during coco pretraining, the sizes of two input frames are different, so we pad them together ant get key frame gt mask for loss calculation
        target_masks, valid = nested_tensor_from_tensor_list(key_frame_masks+ref_frame_masks, 
                                                             size_divisibility=32,
                                                             split=False).decompose()
        target_masks = target_masks[:len(key_frame_masks)]
        target_masks = target_masks.to(src_masks)     
        # downsample ground truth masks with ratio mask_out_stride
        start = int(self.mask_out_stride // 2)
        im_h, im_w = target_masks.shape[-2:]
        
        target_masks = target_masks[:, :, start::self.mask_out_stride, start::self.mask_out_stride]

        assert target_masks.size(2) * self.mask_out_stride == im_h
        assert target_masks.size(3) * self.mask_out_stride == im_w       

        batch_size = len(targets)
        tgt_mask_list = []
        for batch_idx in range(batch_size):
            valid_num = targets[batch_idx]["masks"].shape[0]
            gt_multi_idx = indices[batch_idx][1]
            if len(gt_multi_idx)==0:
                continue 
            batch_masks = target_masks[batch_idx][:valid_num][gt_multi_idx].unsqueeze(1)
            tgt_mask_list.append(batch_masks)
        

        if len(tgt_mask_list) != 0:
            target_masks = torch.cat(tgt_mask_list)
            num_boxes = src_masks.shape[0]
            assert src_masks.shape == target_masks.shape

            # src_masks: bs x [1, num_inst, num_frames, H/4, W/4] or [bs, num_inst, num_frames, H/4, W/4]

            # src_masks: [num_insts, num_frames, H/4, M/4]
            # src_masks = src_masks[src_idx]

            # print(src_masks.shape)
            # print(target_masks.shape)
            # print(num_boxes)
            # torch.Size([18, 1, 104, 176])
            # torch.Size([18, 1, 104, 176])
            # 18
            # torch.Size([30, 1, 144, 192])
            # torch.Size([30, 1, 144, 192])
            # 30
            # exit(0)

            src_masks = src_masks.flatten(1)
            target_masks = target_masks.flatten(1)
            # src_masks/target_masks: [n_targets, num_frames* H * W]

            # print(src_masks.shape)
            # print(target_masks.shape)
            # print(num_boxes)
            # torch.Size([32, 18304])
            # torch.Size([32, 18304])
            # 32
            # torch.Size([33, 21504])
            # torch.Size([33, 21504])
            # 33
            # exit(0)
            # losses = {
            #     "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes),
            #     "loss_dice": dice_loss(src_masks, target_masks, num_boxes),
            # }
            # losses = {
            #     "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes)*0.5,
            #     "loss_dice": dice_loss(src_masks, target_masks, num_boxes)*0.5,
            # }
            losses = {
                "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes)*0.5,
                "loss_dice": dice_loss(src_masks, target_masks, num_boxes)*0.5,
            }
        else:
            losses = {
            "loss_mask": (src_masks*0).sum(),
            "loss_dice": (src_masks*0).sum(),
            }
        return losses
    def loss_boxinst(self, outputs,  targets, ref_target, indices, num_boxes, mask_logits, gt_instances=None):
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        src_masks = outputs["pred_masks"]

        bz = len(gt_instances)//2
        key_ids = list(range(0,bz*2-1,2))
        # ref_ids = list(range(1,bz*2,2))

        det_gt_instances = [gt_instances[_i] for _i in key_ids]
        # ref_gt_instances = [gt_instances[_i] for _i in ref_ids]

        # if type(src_masks) == list:
        #     src_masks = torch.cat(src_masks, dim=1)[0]    
        temp_image_color_similarity_list=[]    
        temp_gt_bitmasks = []
        temp_pred_masks = []
        for indexx,itemm in enumerate(det_gt_instances):
            #gt
            if not itemm.has('gt_bitmasks'):#在某些帧中没有instance 将会出现为空的情况
                continue
            gt_bitmasks = itemm.get('gt_bitmasks')
            image_color_similarity = itemm.get('image_color_similarity')  # [2, 8, 128, 192]
            # .type(torch.long)
            indices_flag = indices[indexx][1].type(torch.long)

            temp_pred_masks.append(src_masks[indexx].squeeze(0))  # [x,1,H,W]  predicted

            image_color_similarity = image_color_similarity[indices_flag].to(dtype=gt_bitmasks.dtype)
            temp_image_color_similarity_list.append(image_color_similarity)

            gt_bitmasks = gt_bitmasks[indices_flag].to(dtype=mask_logits.dtype)
            temp_gt_bitmasks.append(gt_bitmasks)
        
        #防止连续两帧都为空    
        if len(temp_image_color_similarity_list)==0 and len(temp_gt_bitmasks)==0:
            losses = {
                "loss_prj": (mask_logits*0).sum(),
                "loss_pairwise": (mask_logits*0).sum()
            }
            return losses
        image_color_similarity =torch.cat(temp_image_color_similarity_list)
        gt_bitmasks = torch.cat(temp_gt_bitmasks)
        gt_bitmasks = gt_bitmasks.unsqueeze(1)

        pred_masks =torch.cat(temp_pred_masks)
        mask_scores = pred_masks.sigmoid()

        if gt_bitmasks.shape[0]==0:
            losses = {
                "loss_prj": (mask_logits*0).sum(),
                "loss_pairwise": (mask_logits*0).sum()
            }
            return losses
        loss_prj = compute_project_term(mask_scores, gt_bitmasks)

        # self.pairwise_dilation=2
        # self.pairwise_size = 3
        pairwise_losses= compute_pairwise_term(
            pred_masks, self.pairwise_size,
            self.pairwise_dilation
        )
        
        weights = (image_color_similarity >= self.pairwise_color_thresh).float() * gt_bitmasks.float()
        loss_pairwise = (pairwise_losses * weights).sum() / weights.sum().clamp(min=1.0)

        warmup_factor = min(self._iter.item() / float(self._warmup_iters), 1.0)
        loss_pairwise = loss_pairwise * warmup_factor

        losses = {
            "loss_prj": loss_prj,
            "loss_pairwise": loss_pairwise,
        }
        ######################################
        # losses = {
        #     "loss_prj": loss_prj*2,
        #     "loss_pairwise": loss_pairwise*2,
        # }
        ######################################
        return losses
    ################################################################################################################################################################
    # def loss_boxinst(self, outputs,  targets, ref_target, indices, num_boxes, mask_logits, gt_instances=None):
    #     num_boxes = mask_logits.shape[1]

    #     gt_inds=[]

    #     temp_image_color_similarity_list = []
    #     for x in gt_instances:
    #         if x.has('image_color_similarity'):
    #             temp_image_color_similarity_list.append(x.image_color_similarity)
    #     if not len(temp_image_color_similarity_list):
    #         losses = {
    #             "loss_prj": (mask_logits*0).sum(),
    #             "loss_pairwise": (mask_logits*0).sum()
    #         }
    #         return losses
    #     image_color_similarity = torch.cat(temp_image_color_similarity_list)
    #     # image_color_similarity = image_color_similarity[gt_inds].to(dtype=mask_logits.dtype)
    #     every_len = int((num_boxes)/image_color_similarity.shape[0])

    #     for i in range(image_color_similarity.shape[0]):
    #         for _ in range(every_len):
    #                 gt_inds.append(i)
    #     if len(gt_inds) != num_boxes:
    #         for _ in range(int(num_boxes-len(gt_inds))):
    #             gt_inds.append(int(image_color_similarity.shape[0]-1))

    #     temp_gt_bitmasks = []
    #     for x in gt_instances:
    #         if x.has('gt_bitmasks'):
    #             temp_gt_bitmasks.append(x.gt_bitmasks)
    #     if not len(temp_gt_bitmasks):
    #         losses = {
    #             "loss_prj": (mask_logits*0).sum(),
    #             "loss_pairwise": (mask_logits*0).sum()
    #         }
    #         return losses
        
    #     # gt_bitmasks = torch.cat([per_im.gt_bitmasks for per_im in gt_instances])
    #     gt_bitmasks = torch.cat(temp_gt_bitmasks)
    #     gt_bitmasks = gt_bitmasks[gt_inds].unsqueeze(dim=1).to(dtype=mask_logits.dtype)
    #     if gt_bitmasks.shape[0]==0:
    #         losses = {
    #             "loss_prj": (mask_logits*0).sum(),
    #             "loss_pairwise": (mask_logits*0).sum()
    #         }
    #         return losses

    #     image_color_similarity = image_color_similarity[gt_inds].to(dtype=gt_bitmasks.dtype)


    #     # mask_logits = self.mask_heads_forward_with_coords(
    #     #     mask_feats, mask_feat_stride, pred_instances
    #     # )
    #     mask_logits = mask_logits.transpose(1,0)

    #     mask_scores = mask_logits.sigmoid()

    #     # mask_scores = mask_scores.transpose(1,0)

    #     assert mask_scores.shape == gt_bitmasks.shape , f'mask_scores.shape {mask_scores.shape} vs gt_bitmasks.shape {gt_bitmasks.shape}?'
    #     loss_prj_term = compute_project_term(mask_scores, gt_bitmasks)

    #     self.pairwise_dilation=2
    #     self.pairwise_size = 3
    #     pairwise_losses = compute_pairwise_term(
    #         mask_logits, self.pairwise_size,
    #         self.pairwise_dilation
    #     )
    #     # self.pairwise_color_thresh=0.3
    #     weights = (image_color_similarity >= self.pairwise_color_thresh).float() * gt_bitmasks.float()
    #     loss_pairwise = (pairwise_losses * weights).sum() / weights.sum().clamp(min=1.0)

    #     warmup_factor = min(self._iter.item() / float(self._warmup_iters), 1.0)
    #     loss_pairwise = loss_pairwise * warmup_factor
    #     losses = {}
    #     losses.update({
    #         "loss_prj": loss_prj_term,
    #         "loss_pairwise": loss_pairwise,
    #     })
    #     return losses
    def mask_heads_forward_with_coords(
            self, mask_feats, mask_feat_stride, instances
    ):
        locations = compute_locations(
            mask_feats.size(2), mask_feats.size(3),
            stride=mask_feat_stride, device=mask_feats.device
        )
        n_inst = len(instances)

        im_inds = instances.im_inds
        mask_head_params = instances.mask_head_params

        N, _, H, W = mask_feats.size()

        instance_locations = instances.locations
        relative_coords = instance_locations.reshape(-1, 1, 2) - locations.reshape(1, -1, 2)
        relative_coords = relative_coords.permute(0, 2, 1).float()
        soi = self.sizes_of_interest.float()[instances.fpn_levels]
        relative_coords = relative_coords / soi.reshape(-1, 1, 1)
        relative_coords = relative_coords.to(dtype=mask_feats.dtype)

        mask_head_inputs = torch.cat([
            relative_coords, mask_feats[im_inds].reshape(n_inst, self.in_channels, H * W)
        ], dim=1)


        mask_head_inputs = mask_head_inputs.reshape(1, -1, H, W)

        weights, biases = parse_dynamic_params(
            mask_head_params, self.channels,
            self.weight_nums, self.bias_nums
        )

        mask_logits = self.mask_heads_forward(mask_head_inputs, weights, biases, n_inst)

        mask_logits = mask_logits.reshape(-1, 1, H, W)

        assert mask_feat_stride >= self.mask_out_stride
        assert mask_feat_stride % self.mask_out_stride == 0
        mask_logits = aligned_bilinear(mask_logits, int(mask_feat_stride / self.mask_out_stride))

        return mask_logits

    def mask_heads_forward(self, features, weights, biases, num_insts):
        '''
        :param features
        :param weights: [w0, w1, ...]
        :param bias: [b0, b1, ...]
        :return:
        '''
        assert features.dim() == 4
        n_layers = len(weights)
        x = features
        for i, (w, b) in enumerate(zip(weights, biases)):
            x = F.conv2d(
                x, w, bias=b,
                stride=1, padding=0,
                groups=num_insts
            )
            if i < n_layers - 1:
                x = F.relu(x)
        return x

    ################################################################################################################################################################
    def loss_reid(self, outputs,  targets, ref_target, indices, num_boxes):

        qd_items = outputs['pred_qd']
        contras_loss = 0
        aux_loss = 0
        if len(qd_items) == 0:
            losses = {'loss_reid': outputs['pred_logits'].sum()*0,
                   'loss_reid_aux':  outputs['pred_logits'].sum()*0 }
            return losses
        for qd_item in qd_items:
            pred = qd_item['contrast'].permute(1,0)
            label = qd_item['label'].unsqueeze(0)
            # contrastive loss
            pos_inds = (label == 1)
            neg_inds = (label == 0)
            pred_pos = pred * pos_inds.float()
            pred_neg = pred * neg_inds.float()
            # use -inf to mask out unwanted elements.
            pred_pos[neg_inds] = pred_pos[neg_inds] + float('inf')
            pred_neg[pos_inds] = pred_neg[pos_inds] + float('-inf')

            _pos_expand = torch.repeat_interleave(pred_pos, pred.shape[1], dim=1)
            _neg_expand = pred_neg.repeat(1, pred.shape[1])
            # [bz,N], N is all pos and negative samples on reference frame, label indicate it's pos or negative
            x = torch.nn.functional.pad((_neg_expand - _pos_expand), (0, 1), "constant", 0) 
            contras_loss += torch.logsumexp(x, dim=1)

            aux_pred = qd_item['aux_consin'].permute(1,0)
            aux_label = qd_item['aux_label'].unsqueeze(0)

            aux_loss += (torch.abs(aux_pred - aux_label)**2).mean()


        losses = {'loss_reid': contras_loss.sum()/len(qd_items),
                   'loss_reid_aux':  aux_loss/len(qd_items) }

        return losses
    
    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets, ref_target, indices, num_boxes, **kwargs):
        loss_map = {
            'labels': self.loss_labels,
            'cardinality': self.loss_cardinality,
            'boxes': self.loss_boxes,
            'masks': self.loss_masks,
            'reid': self.loss_reid,
            # 'loss_boxinst': self.loss_boxinst
            'no_masks': self.loss_boxinst
            # 'no_masks': self.loss_no_masks
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs,  targets, ref_target, indices, num_boxes, **kwargs)
    
    def forward(self, outputs, targets, ref_target, indices_list, mask_logits=None, mask_feat_stride=8, gt_instances=None):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["labels"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()
        ########################################################################
        self._iter += 1
        ########################################################################
        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            kwargs = {}
            #######################################################
            # if loss == "loss_boxinst":
            if loss == "no_masks":
                # losses.update(self.loss_boxinst(mask_logits, gt_instances, **kwargs))
                # losses.update(self.loss_no_masks(outputs, targets, ref_target, indices_list[-1], num_boxes,gt_instances, **kwargs))
                losses.update(self.loss_boxinst(outputs, targets, ref_target, indices_list[-1], num_boxes,mask_logits,gt_instances, **kwargs))
                continue
            #######################################################
            losses.update(self.get_loss(loss, outputs, targets, ref_target, indices_list[-1], num_boxes, **kwargs))

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                # indices = self.matcher(aux_outputs, targets)
                indices = indices_list[i]
                for loss in self.losses:
                    if loss == 'reid':
                        continue
                    kwargs = {}
                    if loss == 'labels':
                        # Logging is enabled only for the last layer
                        kwargs['log'] = False
                    ##############################################################################################################
                    if loss == "no_masks":
                        l_dict = self.loss_boxinst(aux_outputs, targets, ref_target, indices, num_boxes,mask_logits,gt_instances, **kwargs)
                    else:
                        l_dict = self.get_loss(loss, aux_outputs, targets, ref_target, indices, num_boxes, **kwargs)
                    ##############################################################################################################
                    # l_dict = self.get_loss(loss, aux_outputs, targets, ref_target, indices, num_boxes, **kwargs)
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)
        return losses



class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


