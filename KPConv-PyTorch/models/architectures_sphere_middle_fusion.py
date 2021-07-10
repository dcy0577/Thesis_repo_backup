#
#
#      0=================================0
#      |    Kernel Point Convolutions    |
#      0=================================0
#
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Define network architectures
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Hugues THOMAS - 06/03/2020
#

from models.blocks import *
import numpy as np
from mvpnet.models.mvpnet_3d import FeatureAggregation
from mvpnet.models.unet_resnet34 import UNetResNet34
from mvpnet.ops.group_points import group_points


def p2p_fitting_regularizer(net):

    fitting_loss = 0
    repulsive_loss = 0

    for m in net.modules():

        if isinstance(m, KPConv) and m.deformable:

            ##############
            # Fitting loss
            ##############

            # Get the distance to closest input point and normalize to be independant from layers
            KP_min_d2 = m.min_d2 / (m.KP_extent ** 2)

            # Loss will be the square distance to closest input point. We use L1 because dist is already squared
            fitting_loss += net.l1(KP_min_d2, torch.zeros_like(KP_min_d2))

            ################
            # Repulsive loss
            ################

            # Normalized KP locations
            KP_locs = m.deformed_KP / m.KP_extent

            # Point should not be close to each other
            for i in range(net.K):
                other_KP = torch.cat([KP_locs[:, :i, :], KP_locs[:, i + 1:, :]], dim=1).detach()
                distances = torch.sqrt(torch.sum((other_KP - KP_locs[:, i:i + 1, :]) ** 2, dim=2))
                rep_loss = torch.sum(torch.clamp_max(distances - net.repulse_extent, max=0.0) ** 2, dim=1)
                repulsive_loss += net.l1(rep_loss, torch.zeros_like(rep_loss)) / net.K

    return net.deform_fitting_power * (2 * fitting_loss + repulsive_loss)


class KPFCNN_featureAggre(nn.Module):
    """
    Class defining KPFCNN
    """

    def __init__(self, config, lbl_values, ign_lbls):
        super(KPFCNN_featureAggre, self).__init__()

        ############
        # Parameters
        ############

        # Current radius of convolution and feature dimension
        layer = 0
        r = config.first_subsampling_dl * config.conv_radius

        in_dim_3d = config.in_features_dim_3d # 4
        in_dim_2d = config.in_features_dim_2d # 65

        out_dim = config.first_features_dim
        self.K = config.num_kernel_points
        self.C = len(lbl_values) - len(ign_lbls)

        #####################
        # List Encoder blocks
        #####################

        # Save all block operations in a list of modules
        self.encoder_blocks_3d = nn.ModuleList()
        self.encoder_blocks_2d = nn.ModuleList()

        self.encoder_skip_dims = []
        self.encoder_skips = []

        # Loop over consecutive blocks
        for block_i, block in enumerate(config.architecture):

            # Check equivariance
            if ('equivariant' in block) and (not out_dim % 3 == 0):
                raise ValueError('Equivariant block but features dimension is not a factor of 3')

            # Detect change to next layer for skip connection
            if np.any([tmp in block for tmp in ['pool', 'strided', 'upsample', 'global']]):
                self.encoder_skips.append(block_i)
                self.encoder_skip_dims.append(in_dim_3d+in_dim_2d) # cat the skip feature

            # Detect upsampling block to stop
            if 'upsample' in block:
                break

            # Apply the good block function defining tf ops
            self.encoder_blocks_3d.append(block_decider(block,
                                                    r,
                                                    in_dim_3d,
                                                    out_dim,
                                                    layer,
                                                    config))

            self.encoder_blocks_2d.append(block_decider(block,
                                                        r,
                                                        in_dim_2d,
                                                        out_dim,
                                                        layer,
                                                        config))

            # Update dimension of input from output
            if 'simple' in block:
                in_dim_3d = out_dim // 2
                in_dim_2d = out_dim // 2
            else:
                in_dim_3d = out_dim
                in_dim_2d = out_dim

            # Detect change to a subsampled layer
            if 'pool' in block or 'strided' in block:
                # Update radius and feature dimension for next layer
                layer += 1
                r *= 2
                out_dim *= 2

        #####################
        # List Decoder blocks
        #####################

        # Save all block operations in a list of modules
        self.decoder_blocks = nn.ModuleList()
        self.decoder_concats = []

        # Find first upsampling block
        start_i = 0
        for block_i, block in enumerate(config.architecture):
            if 'upsample' in block:
                start_i = block_i
                break

        # update the concatenated in_dim before first deconder layer
        in_dim = in_dim_3d + in_dim_2d

        # Loop over consecutive blocks
        for block_i, block in enumerate(config.architecture[start_i:]):

            # Add dimension of skip connection concat
            if block_i > 0 and 'upsample' in config.architecture[start_i + block_i - 1]:
                in_dim += self.encoder_skip_dims[layer]
                self.decoder_concats.append(block_i)

            # Apply the good block function defining tf ops
            self.decoder_blocks.append(block_decider(block,
                                                    r,
                                                    in_dim,
                                                    out_dim,
                                                    layer,
                                                    config))

            # Update dimension of input from output
            in_dim = out_dim

            # Detect change to a subsampled layer
            if 'upsample' in block:
                # Update radius and feature dimension for next layer
                layer -= 1
                r *= 0.5
                out_dim = out_dim // 2

        self.head_mlp = UnaryBlock(out_dim, config.first_features_dim, False, 0)
        self.head_softmax = UnaryBlock(config.first_features_dim, self.C, False, 0)


        ################
        # Network Losses
        ################

        # List of valid labels (those not ignored in loss)
        self.valid_labels = np.sort([c for c in lbl_values if c not in ign_lbls])

        # Choose segmentation loss
        if len(config.class_w) > 0:
            class_w = torch.from_numpy(np.array(config.class_w, dtype=np.float32))
            self.criterion = torch.nn.CrossEntropyLoss(weight=class_w, ignore_index=-1)
        else:
            self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.deform_fitting_mode = config.deform_fitting_mode
        self.deform_fitting_power = config.deform_fitting_power
        self.deform_lr_factor = config.deform_lr_factor
        self.repulse_extent = config.repulse_extent
        self.output_loss = 0
        self.reg_loss = 0
        self.l1 = nn.L1Loss()

        ################
        # Feature Aggregation
        ################
        self.feat_aggreg = FeatureAggregation(64)

        ################
        # 2d network
        ################

        self.net_2d = UNetResNet34(20, p=0.5, pretrained=True)
        checkpoint = torch.load('/home/dchangyu/mvpnet/outputs_use/scannet/unet_resnet34/model_080000.pth',
                                map_location=torch.device("cpu"))
        self.net_2d.load_state_dict(checkpoint['model'])

        # build freezer for 2d network
        for name, params in self.net_2d.named_parameters():
            params.requires_grad = False
        for name, m in self.net_2d._modules.items():
            m.train(False)
        # self.net_2d.cpu()

        return

    def forward(self, batch, config):

        # (batch_size, num_views, 3, h, w)
        images = batch.images
        b, nv, k, h, w = images.size()
        # collapse first 2 dimensions together
        images = images.reshape([-1] + list(images.shape[2:]))

        # 2D network
        preds_2d = self.net_2d({'image': images})
        feature_2d = preds_2d['feature']  # (b * nv, c, h, w) requires_grad=false
        # reshape 2d feature
        feature_2d = feature_2d.reshape(b, nv, -1, h, w).transpose(1, 2).contiguous()  # (b, c, nv, h, w)
        feature_2d = feature_2d.reshape(b, -1, nv * h * w)  #(b, 64, nv * h * w) # c=64 output channels
        feature_2d_list = []
        # reshape image_xyz
        image_xyz = batch.image_xyz  # (b, nv, h, w, 3)
        image_xyz = image_xyz.permute(0, 4, 1, 2, 3).reshape(b, 3, nv * h * w)  #(b, 3, nv * h * w)
        image_xyz_list = []

        knn_list = batch.knn_list
        for i in range(b):
            # unproject 2d feature for each scene
            batch_knn_indices = torch.from_numpy(knn_list[i]).long().cuda() #(1, s_np, k) #knn_indices of scene_i
            batch_feature_2d = feature_2d[i, :, :].unsqueeze(0) #(1, 64, nv * h * w) # 2d feature of scene_i
            batch_feature_2d = group_points(batch_feature_2d, batch_knn_indices)  # (1, 64, s_np, k) grouped points for scene_i
            feature_2d_list.append(batch_feature_2d)

            # unproject depth maps for each scene
            with torch.no_grad():
                batch_image_xyz = image_xyz[i, :, :].unsqueeze(0) #(1, 3, nv * h * w) # image_xyz of scene_i
                batch_image_xyz = group_points(batch_image_xyz, batch_knn_indices)  # (1, 3, s_np, k) grouped points for scene_i
                image_xyz_list.append(batch_image_xyz)

        # seen these two as point-wise feature and stacked them in point dims to adapt size of input points
        input_feature_2d = torch.cat(feature_2d_list,dim=2) # (1, c, np, k) # 2d feature of one large stacked batch
        input_image_xyz = torch.cat(image_xyz_list, dim=2) # (1, 3, np, k) # image_xyz of one large stacked batch

        # 2D-3D aggregation
        points = batch.feat_aggre_points.transpose(1,2)  # (1,3,np) # input points of one large stacked batch
        feature_2d3d = self.feat_aggreg(input_image_xyz, points, input_feature_2d)  # (1,64,np)
        feature_2d3d = feature_2d3d.permute(0,2,1).reshape(-1,64) # (np,64)

        # stack the features with constant 1 to insure black/dark points are not ignored
        stacked_features = torch.ones_like(batch.feat_aggre_points[:, :, :1].squeeze(0)) # (np,1)
        # stack the 3d color feature
        stacked_features_3d = torch.cat((stacked_features, batch.colors), dim=1)  # (np,4) feature dim = 3+1

        # 2d3d feature
        # stacked_features_2d = torch.cat((stacked_features, feature_2d3d),dim=1) # (np,65) feature dim = 64+1
        stacked_features_2d = feature_2d3d # feature dim = 64

        # Get input features
        x_2d = stacked_features_2d.clone().detach() # feature dim: 64
        x_3d = stacked_features_3d.clone().detach() # feature dim: 4


        # Loop over consecutive blocks

        # middle fusion
        skip_x = []
        for block_i, block_op in enumerate(self.encoder_blocks_3d):
            if block_i in self.encoder_skips:
                skip_x.append(x_3d) # indim
            x_3d = block_op(x_3d, batch)

        index = 0
        for block_i, block_op in enumerate(self.encoder_blocks_2d):
            if block_i in self.encoder_skips:
                skip_x[index] = torch.cat([skip_x[index], x_2d], dim=1) #cat the skip feature
                index += 1
            x_2d = block_op(x_2d, batch)

        # before decoder, fuse the features
        # x = torch.cat([x_3d, x_2d], dim=1) # indim*2
        x = torch.mean(torch.stack([x_3d,x_2d]),0)

        for block_i, block_op in enumerate(self.decoder_blocks):
            if block_i in self.decoder_concats:
                x = torch.cat([x, skip_x.pop()], dim=1)
            x = block_op(x, batch)

        # Head of network
        x = self.head_mlp(x, batch)
        x = self.head_softmax(x, batch)

        return x

    def loss(self, outputs, labels):
        """
        Runs the loss on outputs of the model
        :param outputs: logits
        :param labels: labels
        :return: loss
        """

        # Set all ignored labels to -1 and correct the other label to be in [0, C-1] range
        target = - torch.ones_like(labels)
        for i, c in enumerate(self.valid_labels):
            target[labels == c] = i

        if torch.equal(target, labels):
            print('ok')

        # Reshape to have a minibatch size of 1
        outputs = torch.transpose(outputs, 0, 1)
        outputs = outputs.unsqueeze(0)
        target = target.unsqueeze(0)

        # Cross entropy loss
        self.output_loss = self.criterion(outputs, target)

        # Regularization of deformable offsets
        if self.deform_fitting_mode == 'point2point':
            self.reg_loss = p2p_fitting_regularizer(self)
        elif self.deform_fitting_mode == 'point2plane':
            raise ValueError('point2plane fitting mode not implemented yet.')
        else:
            raise ValueError('Unknown fitting mode: ' + self.deform_fitting_mode)

        # Combined loss
        return self.output_loss + self.reg_loss

    def accuracy(self, outputs, labels):
        """
        Computes accuracy of the current batch
        :param outputs: logits predicted by the network
        :param labels: labels
        :return: accuracy value
        """

        # Set all ignored labels to -1 and correct the other label to be in [0, C-1] range
        target = - torch.ones_like(labels)
        for i, c in enumerate(self.valid_labels):
            target[labels == c] = i

        predicted = torch.argmax(outputs.data, dim=1)
        total = target.size(0)
        correct = (predicted == target).sum().item()

        return correct / total





















