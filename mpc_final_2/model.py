''' Model Definition '''
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import drn
import dla
from dla_up import DLAUp, Identity, fill_up_weights, BatchNorm
import dla_up
import numpy as np
import math
import pdb
from end_layer import end_layer
from conv_lstm import convLSTM
from fcn import fcn
from utils import weights_init

class atari_model(nn.Module):
    def __init__(self, inc=12, num_actions=9, frame_history_len=4):
        super(atari_model, self).__init__()
        self.conv1 = nn.Conv2d(inc, 32, kernel_size=8, stride=4)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.conv3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.fc4 = nn.Linear(7 * 7 * 64, 512)
        self.fc5 = nn.Linear(512, num_actions)
        self.frame_history_len = frame_history_len
        
    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.fc4(x.view(x.size(0), -1)))
        res = self.fc5(x)
        return res

class ConvLSTMCell(nn.Module):
    def __init__(self, input_dim, hidden_dim, bias):
        super(ConvLSTMCell, self).__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.bias = bias
        self.fc1 = nn.Linear(input_dim + hidden_dim, hidden_dim, bias = self.bias)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim, bias = self.bias)
        self.fc3 = nn.Linear(hidden_dim, hidden_dim, bias = self.bias)
        self.fc4 = nn.Linear(hidden_dim, hidden_dim, bias = self.bias)
        self.W = nn.Linear(hidden_dim, 4 * hidden_dim, bias = self.bias)
        
    def forward(self, input_tensor, cur_state):
        h_cur, c_cur = cur_state
        combined = torch.cat([input_tensor, h_cur], dim = 1)
        x = F.relu(self.fc1(combined))
        x = F.relu(self.fc2(x))
        x = F.relu(self.fc3(x))
        x = F.relu(self.fc4(x))
        combined_conv = F.relu(self.W(x))
        cc_i, cc_f, cc_o, cc_g = torch.split(combined_conv, self.hidden_dim, dim = 1) 
        i = torch.sigmoid(cc_i)
        f = torch.sigmoid(cc_f)
        o = torch.sigmoid(cc_o)
        g = torch.tanh(cc_g)
        c_next = f * c_cur + i * g
        h_next = o * torch.tanh(c_next)
        return h_next, c_next # h_next is the output

    def init_hidden(self, batch_size):
        use_cuda = torch.cuda.is_available()
        if use_cuda:
            return (Variable(torch.zeros(batch_size, self.hidden_dim)).cuda(),
                    Variable(torch.zeros(batch_size, self.hidden_dim)).cuda())
        else:
            return (Variable(torch.zeros(batch_size, self.hidden_dim)),
                    Variable(torch.zeros(batch_size, self.hidden_dim)))

class DRNSeg(nn.Module):
    ''' Network For Feature Extraction for Segmentation Prediction '''
    def __init__(self, args):
        super(DRNSeg, self).__init__()
        model = drn.__dict__.get(args.drn_model)(pretrained = args.pretrained, num_classes = 1000)
        self.model = nn.Sequential(*list(model.children())[:-2])
        self.seg = nn.Conv2d(model.out_dim, args.classes, kernel_size=1, bias=True)

    def forward(self, x):
        x = self.model(x)
        feature_map = self.seg(x)
        return feature_map # size: batch_size x classes x 32 x 32

class DLASeg(nn.Module):
    def __init__(self, args, down_ratio=2):
        super(DLASeg, self).__init__()
        assert down_ratio in [2, 4, 8, 16]
        self.first_level = int(np.log2(down_ratio))
        self.base = dla.__dict__[args.drn_model](pretrained=args.pretrained,
                                            return_levels=True)
        channels = self.base.channels
        scales = [2 ** i for i in range(len(channels[self.first_level:]))]
        self.dla_up = DLAUp(channels[self.first_level:], scales=scales)
        self.fc = nn.Sequential(
            nn.Conv2d(channels[self.first_level], args.classes, kernel_size=1,
                      stride=1, padding=0, bias=True)
        )
        up_factor = 2 ** self.first_level
        if up_factor > 1:
            up = nn.ConvTranspose2d(args.classes, args.classes, up_factor * 2,
                                    stride=up_factor, padding=up_factor // 2,
                                    output_padding=0, groups=args.classes,
                                    bias=False)
            fill_up_weights(up)
            up.weight.requires_grad = False
        else:
            up = Identity()
        self.up = up
        self.softmax = nn.LogSoftmax(dim=1)

        for m in self.fc.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, BatchNorm):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x):
        x = self.base(x)
        x = self.dla_up(x[self.first_level:])
        x = self.fc(x)
        x = self.up(x)
        # y = self.softmax(x)
        return x

    def optim_parameters(self, memo=None):
        for param in self.base.parameters():
            yield param
        for param in self.dla_up.parameters():
            yield param
        for param in self.fc.parameters():
            yield param

class ConvLSTMNet(nn.Module):
    def __init__(self, args):
        super(ConvLSTMNet, self).__init__()
        self.args = args

        # feature extraction part
        if args.use_seg:
            self.action_encode = nn.Linear(args.num_total_act, 1*32*32) 
            if not args.use_lstm:
                self.actionEncoder = nn.Linear(args.num_total_act, 32)
            self.drnseg = DLASeg(args)
            self.action_up = nn.Linear(32*32, 256*256)
            self.feature_map_predictor = convLSTM(args.classes * args.frame_history_len + 1, args.classes) if args.use_lstm else fcn(args.classes * args.frame_history_len, args.classes)
            #self.up_sampler = lambda x: F.upsample(x, scale_factor = 8, mode = 'bilinear', align_corners = True)
        else:
            self.action_encode = nn.Linear(args.num_total_act, args.info_dim)
            self.info_encode = nn.Linear(args.info_dim + args.hidden_dim, args.hidden_dim)
            self.dla = dla.dla46x_c(pretrained = args.pretrained)
            self.feature_encode = nn.Linear(256 * args.frame_history_len, args.hidden_dim)
            self.lstm = ConvLSTMCell(args.hidden_dim, args.hidden_dim, True)
            self.outfeature_encode = nn.Linear(args.hidden_dim + args.info_dim, args.hidden_dim)
        
        # output layers
        self.coll_layer = end_layer(args, 2, nn.Softmax(dim = -1))
        self.off_layer = end_layer(args, 2, nn.Softmax(dim = -1))
        self.dist_layer = end_layer(args, 1)

        # optional layers
        if args.use_pos:
            self.pos_layer = end_layer(args, 1)
        if args.use_angle:
            self.angle_layer = end_layer(args, 1)
        if args.use_speed:
            self.speed_layer = end_layer(args, 1)
        if args.use_xyz:
            self.xyz_layer = end_layer(args, 3)

    def forward(self, x, action, with_encode=False, hidden=None, cell=None, training=True):
        if with_encode == False:
            x = self.get_feature(x)
        if hidden is None or cell is None:
            if self.args.use_seg and self.args.use_lstm:
                shape = [x.size(0), self.args.classes, 32, 32]
                hidden = x[:, -self.args.classes:, :, :] # Variable(torch.zeros(shape))
                cell = x[:, -self.args.classes:, :, :] # Variable(torch.zeros(shape))
            else:
                hidden = x # Variable(torch.zeros(x.size()))
                cell = x # Variable(torch.zeros(x.size()))

        output_dict = dict()
        if self.args.use_seg:
            action_enc = self.action_encode(action).view(-1, 1, 32, 32)
            if self.args.use_lstm:
                combined = torch.cat([action_enc, x], dim = 1)
                hidden, cell = self.feature_map_predictor(combined, (hidden, cell))
            else:
                action_encoding = self.actionEncoder(action)
                hidden = self.feature_map_predictor(x, action_encoding)
            action_enc = self.action_up(action_enc.view(-1, 32*32)).view(-1, 1, 256, 256)
            if with_encode == False:
                output_dict['seg_current'] = x[:, -self.args.classes:, :, :]
            output_dict['seg_pred'] = hidden
            feature_enc = torch.cat([x[:, self.args.classes:, :, :], hidden], dim = 1)
            nx_feature_enc = feature_enc.detach() if training else feature_enc
        else:
            action_enc = F.relu(self.action_encode(action))
            encode = torch.cat([x, action_enc], dim = 1)
            encode = F.relu(self.info_encode(encode))
            hidden, cell = self.lstm(encode, [hidden, cell])
            nx_feature_enc = hidden#.view(-1, self.args.hidden_dim)
            output_dict['seg_pred'] = self.outfeature_encode(F.relu(torch.cat([nx_feature_enc, action_enc], dim = 1)))
       
        # major outputs
        output_dict['coll_prob'] = self.coll_layer(nx_feature_enc, action_enc)
        output_dict['offroad_prob'] = self.off_layer(nx_feature_enc, action_enc)
        output_dict['dist'] = self.dist_layer(nx_feature_enc, action_enc)

        # optional outputs
        if self.args.use_pos:
            output_dict['pos'] = self.pos_layer(nx_feature_enc, action_enc)
        if self.args.use_angle:
            output_dict['angle'] = self.angle_layer(nx_feature_enc, action_enc)
        if self.args.use_speed:
            output_dict['speed'] = self.speed_layer(nx_feature_enc, action_enc)
        if self.args.use_xyz:
            output_dict['xyz'] = self.xyz_layer(nx_feature_enc, action_enc)
        
        if self.args.use_seg:
            nx_feature_enc = feature_enc
        return output_dict, nx_feature_enc, hidden, cell
     
    def get_feature(self, x):
        res = []
        batch_size = x.size(0)
        if self.args.use_seg:
            res = torch.cat([self.drnseg(x[:, i*3 : (i+1)*3, :, :]) for i in range(self.args.frame_history_len)], dim = 1)
        else:
            for i in range(self.args.frame_history_len):
                out = self.dla(x[:, i*3 : (i+1)*3, :, :])
                out = out.squeeze().view(batch_size, -1)
                res.append(out)
            res = torch.cat(res, dim=1)
            res = self.feature_encode(res)
        return res 

class ConvLSTMMulti(nn.Module):
    def __init__(self, args):
        super(ConvLSTMMulti, self).__init__()
        self.args = args
        self.conv_lstm = ConvLSTMNet(self.args)

    def get_feature(self, x):
        x = x.contiguous()
        num_time = int(x.size()[1])

        feat = [self.conv_lstm.get_feature(x[:, i, :, :, :].squeeze(1)) for i in range(num_time)]

        return torch.stack(feat, dim=1)

    def forward(self, imgs, actions=None, hidden=None, cell=None, get_feature=False, training=True):
        if get_feature:
            return self.get_feature(imgs)

        batch_size, num_step, c, w, h = int(imgs.size()[0]), int(imgs.size()[1]), int(imgs.size()[-3]), int(imgs.size()[-2]), int(imgs.size()[-1])
        output_dict, pred, hidden, cell = self.conv_lstm(imgs[:,0,:,:,:].squeeze(1), actions[:,0,:].squeeze(1), hidden=hidden, cell=cell, training=training)
        
        # create dictionary to store outputs
        final_dict = dict()
        for key in output_dict.keys():
            final_dict[key] = [output_dict[key]]
        if self.args.use_seg:
            final_dict['seg_pred'] = [output_dict['seg_current'], output_dict['seg_pred']]

        for i in range(1, self.args.pred_step):
            output_dict, pred, hidden, cell = self.conv_lstm(pred, actions[:, i, :], with_encode=True, hidden=hidden, cell=cell, training=training)
            for key in output_dict.keys():
                final_dict[key].append(output_dict[key])

        for key in final_dict.keys():
            final_dict[key] = torch.stack(final_dict[key], dim=1)

        return final_dict

class Planner(nn.Module):
    def __init__(self, args):
        super(Planner, self).__init__()
        self.args = args

        self.conv1 = nn.Conv2d(args.classes * args.frame_history_len, 16, 5, stride=2, padding=1)
        self.conv2 = nn.Conv2d(16, 32, 3, stride=2, padding=1)
        self.conv3 = nn.Conv2d(32, 64, 3, stride=2, padding=1)
        self.conv4 = nn.Conv2d(64, 32, 1, stride=1, padding=0)

        self.fc1 = nn.Linear(512, 128)
        self.fc2 = nn.Linear(128, 32)

        self.fc3 = nn.Linear(64, 16)
        self.fc4 = nn.Linear(16, 16)
        self.fc3 = nn.Linear(16, 2)
        self.apply(weights_init)

    def extract_feature(self, x):
        x = F.tanh(F.max_pool2d(self.conv1(x), kernel_size=2, stride=2))
        x = F.tanh(F.max_pool2d(self.conv2(x), kernel_size=2, stride=2))
        x = F.tanh(F.max_pool2d(self.conv3(x), kernel_size=2, stride=2)) # 1x64x4x4
        x = F.tanh(self.conv4(x))
        x = x.view(x.size(0), -1)

        x = F.tanh(self.fc1(x))
        return F.tanh(self.fc2(x))

    def forward(self, x1, x2):
        x1 = self.extract_feature(x1)
        x2 = self.extract_feature(x2)
        x = torch.cat([x1, x2], dim=1)
        x = F.tanh(self.fc3(x))
        x = F.tanh(self.fc4(x))
        return F.tanh(self.fc5(x))

if __name__ == '__main__':
    net = ConvLSTMMulti(num_actions=2, pretrained=True, frame_history_len=4, use_seg=True,
                      use_xyz=True, model_name='drn_d_22', num_classes=4, hidden_dim=512, info_dim=16)
    action = Variable(torch.zeros(16, 2, 2), requires_grad=False)
    img = Variable(torch.zeros(16, 2, 12, 256, 256), requires_grad=False)
    result = net(img, action)
    for i in result:
        print(i.size())