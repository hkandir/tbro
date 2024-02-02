from typing import List, Optional, Tuple

import torch
import torch.nn as nn
from torch.nn import TransformerEncoderLayer as TorchTFL
import pytorch_lightning as pl

from scripts.models.deepro_enc_reduced import deepROEncoder
from scripts.models.deepro_dec import deepRODecoder
from scripts.models.deepro_pos_enc import PositionalEmbedding
from scripts.utils import weight_init
from scripts.utils.params import Parameters
from scripts.utils.losses import traj_and_odom_loss

from einops import rearrange, repeat, reduce
from einops.layers.torch import Rearrange

# class SaveOutput:
#     def __init__(self):
#         self.outputs = []

#     def __call__(self, module, module_in, module_out):
#         self.outputs.append(module_out[1])

#     def clear(self):
#         self.outputs = []

class TransformerEncoderLayer(TorchTFL):
    def _sa_block(self, x,
                  attn_mask, key_padding_mask):
        x = self.self_attn(x, x, x,
                           attn_mask=attn_mask,
                           key_padding_mask=key_padding_mask,
                           need_weights=True)[0]
        return self.dropout1(x)
    
class DeepROTransformer(pl.LightningModule):
    def __init__(
        self, args: Parameters
    ):
        super().__init__()

        self.save_hyperparameters(
            {
            'sequence_length': args.max_length,
		    'batch_size': args.batch_size,
		    'epochs': args.epochs,
		    'radar_shape': args.radar_shape,
		    'hidden_size': args.hidden_size,
		    'learning_rate': args.learning_rate,
            'alphas': args.alphas,
            'betas': args.betas,
            'gammas': args.gammas
            }
        )
        self.learning_rate = args.learning_rate
        self.batch_size = args.batch_size
        self.seq_length = args.max_length
        self.dim = args.hidden_size

        self.visualization = {}

        # self.automatic_optimization = False

        self.mean = args.mean_enable
        self.learning_rate = args.learning_rate

        self.encoder = deepROEncoder(in_channels=2, max_pool = True) # Two radar images (power channel only)
        if self.mean:
            self._cnn_feature_vector_size = self.encoder.conv4_2[0].out_channels
        else:
            self._cnn_feature_vector_size = 4*8*4*256
        
        ################################# Above Encoding Same as Before #####################################
        
        # Image shape prior to patching Batch, Seq, H, W, D, Channel = self.batch_size, self.seq_length, 4, 8, 4, 256
        image_height, image_width, image_depth = 4, 8, 4
        patch_height, patch_width, patch_depth = 2, 2, 2
        frame_patch_size = self.seq_length // 2
        channels = self.encoder.conv4_2[0].out_channels # 256

        self.patch_dim = channels * patch_height * patch_width * patch_depth * frame_patch_size

        num_image_patches = (image_height // patch_height) * (image_width // patch_width) * (image_depth // patch_depth)
        num_frame_patches = (self.seq_length // frame_patch_size)

        # Can also use cls tokens but we are not doing classiffication
        self.global_average_pool = True        
        
        self.to_patch_embedding = nn.Sequential(
            Rearrange('b (seq, pf) c (h ph) (w pw) (d pd) -> b f (h w d) (pw ph pd pf c)', pw = patch_width, ph = patch_height, pd = patch_depth, pf = frame_patch_size),
            nn.LayerNorm(self.patch_dim),
            nn.Linear(self.patch_dim, self.dim),
            nn.LayerNorm(self.dim)
        )

        self.position_embedding = PositionalEmbedding(num_frame_patches = num_frame_patches, num_image_patches = num_image_patches, dim = self.dim)
        # self.position_encoding = PositionalEncoding(d_model=self._cnn_feature_vector_size)          
        

        # d_model = self._cnn_feature_vector_size (Is this true after the patch embedding? I think the answer is no.)
        # Adjusted to d_model = self.patch_dim
        self.spatial_transformer_cross = nn.TransformerEncoder(
            encoder_layer=TransformerEncoderLayer(
                d_model=self.patch_dim, nhead=8, dim_feedforward=args.hidden_size, batch_first=True,
                norm_first=True
            ),
            num_layers=2,
            norm=nn.LayerNorm(normalized_shape=self.patch_dim)
        )
        self.spatial_transformer_cross.layers[-1].self_attn.register_forward_hook(self.hook_fn)

        self.temporal_transformer_cross = nn.TransformerEncoder(
            encoder_layer=TransformerEncoderLayer(
                d_model=self.patch_dim, nhead=8, dim_feedforward=args.hidden_size, batch_first=True,
                norm_first=True
            ),
            num_layers=2,
            norm=nn.LayerNorm(normalized_shape=self.patch_dim)
        )
        self.temporal_transformer_cross.layers[-1].self_attn.register_forward_hook(self.hook_fn)
     
        self._decoder = deepRODecoder(self.patch_dim)
        self.decoder = nn.Sequential(
            nn.Dropout(),
            self._decoder
        )

        self.loss_func = traj_and_odom_loss(args.alphas, args.betas, args.gammas)

        if args.pretrained_enc_path is not None:
            self.encoder.load_weights(args.pretrained_enc_path)

    #     self.save_output = SaveOutput()

    #     for module in self.modules():
    #         print(module)
    #         print('++++++++++++++++++++++++++++++++++++++')
    #         if isinstance(module, nn.MultiheadAttention):
    #             print('Found attention')
    #             self.patch_attention(module)
    #             module.register_forward_hook(self.save_output)

    # def patch_attention(self,module_in):
    #     forward_orig = module_in.forward

    #     def wrap(*args, **kwargs):
    #         kwargs['need_weights'] = True
    #         kwargs['average_attn_weights'] = False

    #         return forward_orig(*args, **kwargs)

    #     module_in.forward = wrap


    def hook_fn(self,m,i,o):
        self.visualization[m] = o

    def make_pairs(self, radar_images: List[torch.Tensor]) -> torch.Tensor:
        tensor = torch.stack(radar_images, dim=1)
        return torch.cat([tensor[:,:-1], tensor[:,1:]],dim=2)

    def forward_cnn(self, pairs: torch.Tensor) -> torch.Tensor:
        # batch_size, sequence_length, channels = 2, height = 64, width = 128, depth = 64
        batch_size, time_steps, C, H, W, D = pairs.size()
        c_in = pairs.view(batch_size*time_steps, C, H, W, D)
        encoded_radar_images = self.encoder(c_in)
        bt_size, C_out, _, _, _ = encoded_radar_images.size()

        cnn_out = rearrange(encoded_radar_images, '(b f) c h w d -> b f c h w d', b = batch_size, f = time_steps)
        # cnn_out = encoded_radar_images.view(batch_size,time_steps,C_out,-1,-1,-1)
        
        # Either use the mean across height/width/depth or flatten height/width/depth into a single feature vector
        # if self.mean:
        #     cnn_out = torch.mean(encoded_radar_images, dim=[2,3,4]).view(batch_size, time_steps, C_out)
        # else:
        #     cnn_out = encoded_radar_images.view(batch_size,time_steps,self._cnn_feature_vector_size)
        
        return cnn_out
    
    def forward_seq(self, seq: torch.Tensor) -> torch.Tensor:
        seq = self.to_patch_embedding(seq)

        position_embedded_seq = self.position_embedding(seq)

        position_embedded_seq = rearrange(position_embedded_seq, 'b f n d -> (b f) n d')

        # Spatial Attention
        spatially_attended_seq = self.spatial_transformer_cross(position_embedded_seq)
        spatially_attended_seq = rearrange(spatially_attended_seq, '(b f) n d -> b f n d', b = self.batch_size)

        # Remove Spatial tokens for Temporal Transformer
        spatially_attended_seq = reduce(spatially_attended_seq, 'b f n d -> b f d', 'mean')

        # Temporal Attention
        temporally_attended_seq = self.temporal_transformer_cross(spatially_attended_seq)

        # Average pool attention
        # Removed because I don't actually want to reduce across frames since I'm not classifying vid but instead tracking odom
        # transformer_output = reduce(temporally_attended_seq, 'b f d -> b d', 'mean')
        
        return temporally_attended_seq

    def decode(self, seq: torch.Tensor) -> torch.Tensor:
        decoded_seq = self.decoder(seq)
        return decoded_seq
    
    def forward(self, radar_images: List[torch.Tensor]) -> torch.Tensor:
        coords = self.decode(self.forward_seq(self.forward_cnn(self.make_pairs(radar_images))))
        return coords

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.learning_rate)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=25, gamma=0.1)
        return [optimizer], [scheduler]
    
    def training_step(self,train_batch,batch_idx):
        seq_len, radar_data, positions, orientations = train_batch[0], train_batch[1], train_batch[2], train_batch[3]

        # opt = self.optimizers()
        # sch = self.lr_schedulers()
        # opt.zero_grad()

        y_hat = self.forward(radar_data)
        loss = self.loss_func(y_hat,positions,orientations)
        self.log('Training_loss',loss,sync_dist=True)

        # self.manual_backward(loss)
        # opt.step()

        # sch.step()
        return loss
    
    def validation_step(self,val_batch,batch_idx):
        seq_len, radar_data, positions, orientations = val_batch[0], val_batch[1], val_batch[2], val_batch[3]
                       
        y_hat = self.forward(radar_data)
        loss = self.loss_func(y_hat,positions,orientations)
        self.log('Validation_loss',loss,sync_dist=True)
        # for key in self.visualization.keys():
        #     print(self.visualization[key][0].size())
        #     print(self.visualization[key][1].size())

        outputs = {"loss": loss, 
                "y_hat": y_hat,
                "positions": positions, 
                "orientations": orientations
                }

        return outputs

    def test_step(self,val_batch,batch_idx):
        seq_len, radar_data, positions, orientations = val_batch[0], val_batch[1], val_batch[2], val_batch[3]

        y_hat = self.forward(radar_data)

        # print("Radar Tensor Shape: ", len(radar_data), radar_data[0].shape)
        # print("Positions Tensor Shape: ", len(positions), positions[0].shape)
        # print("Orientations Tensor Shape: ", len(orientations), orientations[0].shape)
        # print("Prediction Tensor Shape: ", y_hat.shape)
        loss = self.loss_func(y_hat,positions,orientations)
        self.log('Test_loss',loss,sync_dist=True)
        # for key in self.visualization.keys():
        #     print(self.visualization[key][0].size())
        #     print(self.visualization[key][1].size())

        outputs = {"loss": loss, 
                "y_hat": y_hat,
                "positions": positions, 
                "orientations": orientations
                }

        return outputs