# --------------------------------------------------------
# Pytorch Multi-GPU Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time

import cv2

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import pickle
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
# from model.nms.nms_wrapper import nms
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections

from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet

try:
  xrange          # Python 2
except NameError:
  xrange = range  # Python 3


def parse_args():
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
  parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='SNUBH_BUS', type=str)
  parser.add_argument('--cfg', dest='cfg_file',
                      help='optional config file',
                      default='cfgs/vgg16.yml', type=str)
  parser.add_argument('--net', dest='net',
                      help='vgg16, res50, res101, res152',
                      default='res101', type=str)
  parser.add_argument('--set', dest='set_cfgs',
                      help='set config keys', default=None,
                      nargs=argparse.REMAINDER)
  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models', default="models",
                      type=str)
  parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      action='store_true')
  parser.add_argument('--ls', dest='large_scale',
                      help='whether use large imag scale',
                      action='store_true')
  parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
  parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')
  parser.add_argument('--parallel_type', dest='parallel_type',
                      help='which part of model to parallel, 0: all, 1: model before roi pooling',
                      default=0, type=int)
  parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=1, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=10021, type=int)
  parser.add_argument('--vis', dest='vis',
                      help='visualization mode',
                      action='store_true')
  parser.add_argument('--al', dest='active_learning',
                      help='whether to write active learning files',
                      default=0, type=float)
  args = parser.parse_args()
  return args

lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

if __name__ == '__main__':

  args = parse_args()

  print('Called with args:')
  print(args)

  if torch.cuda.is_available() and not args.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

  np.random.seed(cfg.RNG_SEED)
  if args.dataset == "imagenet":
    args.imdb_name = "imagenet_train"
    args.imdbval_name = "imagenet_val"
    args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "SNUBH_BUS":
    args.imdb_name = "SNUBH_BUS"
    args.imdbval_name = "SNUBH_BUS"
    args.set_cfgs = ['ANCHOR_SCALES','[8, 16, 32]','ANCHOR_RATIOS','[0.5, 1, 2]','MAX_NUM_GT_BOXES','20']
  elif args.dataset == "Stanford_Dog":
    args.imdb_name = "Stanford_Dog"
    args.imdbval_name = "Stanford_Dog"
    args.set_cfgs = ['ANCHOR_SCALES', '[8,16,32]', 'ANCHOR_RATIOS','[0.5, 1, 2]', 'MAX_NUM_GT_BOXES', '20']


  args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net)

  if args.cfg_file is not None:
    cfg_from_file(args.cfg_file)
  if args.set_cfgs is not None:
    cfg_from_list(args.set_cfgs)

  print('Using config:')
  pprint.pprint(cfg)

  cfg.TRAIN.USE_FLIPPED = False
  imdb_s, roidb_s, ratio_list_s, ratio_index_s = combined_roidb(args.imdbval_name+'_test', False)
  if args.active_learning:
    imdb_ws, roidb_ws, ratio_list_ws, ratio_index_ws = combined_roidb(args.imdbval_name+'_ws_test', False)
  if args.imdbval_name != "Stanford_Dog":
    imdb_n, roidb_n, ratio_list_n, ratio_index_n = combined_roidb(args.imdbval_name+'_test_normal', False)

  print('{:d} strong roidb entries'.format(len(roidb_s)))
  if args.active_learning:
    print('{:d} weak roidb entries'.format(len(roidb_ws)))
  if args.imdbval_name != "Stanford_Dog":
    print('{:d} normal roidb entries'.format(len(roidb_n)))

  input_dir = args.load_dir + "/" + args.net + "/" + args.dataset
  if not os.path.exists(input_dir):
    raise Exception('There is no input directory for loading network from ' + input_dir)
  load_name = os.path.join(input_dir,
    'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))

  # initilize the network here.
  if args.net == 'vgg16':
    fasterRCNN = vgg16(imdb_s.classes, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res101':
    fasterRCNN = resnet(imdb_s.classes, 101, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res50':
    fasterRCNN = resnet(imdb_s.classes, 50, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res152':
    fasterRCNN = resnet(imdb_s.classes, 152, pretrained=False, class_agnostic=args.class_agnostic)
  else:
    print("network is not defined")
    pdb.set_trace()

  fasterRCNN.create_architecture()

  print("load checkpoint %s" % (load_name))
  checkpoint = torch.load(load_name)
  fasterRCNN.load_state_dict(checkpoint['model'])
  if 'pooling_mode' in checkpoint.keys():
    cfg.POOLING_MODE = checkpoint['pooling_mode']


  print('load model successfully!')
  # initilize the tensor holder here.
  im_data = torch.FloatTensor(1)
  im_info = torch.FloatTensor(1)
  num_boxes = torch.LongTensor(1)
  gt_boxes = torch.FloatTensor(1)
  im_label = torch.FloatTensor(1)

  # ship to cuda
  if args.cuda:
    im_data = im_data.cuda()
    im_info = im_info.cuda()
    num_boxes = num_boxes.cuda()
    gt_boxes = gt_boxes.cuda()
    im_label = im_label.cuda()

  # make variable
  im_data = Variable(im_data)
  im_info = Variable(im_info)
  num_boxes = Variable(num_boxes)
  gt_boxes = Variable(gt_boxes)
  im_label = Variable(im_label)

  if args.cuda:
    cfg.CUDA = True

  if args.cuda:
    fasterRCNN.cuda()

  start = time.time()
  max_per_image = 100

  vis = args.vis

  if vis:
    thresh = 0.05
  else:
    thresh = 0.0

  save_name = 'faster_rcnn_10'
  num_images_s = len(imdb_s.image_index)
  if args.active_learning:
    num_images_ws = len(imdb_ws.image_index)
  if args.imdbval_name != "Stanford_Dog":
    num_images_n = len(imdb_n.image_index)

  all_boxes = [[[] for _ in range(num_images_s)]
               for _ in range(imdb_s.num_classes)]
  if args.active_learning:
    all_boxes_ws = [[[] for _ in range(num_images_ws)]
                for _ in range(imdb_ws.num_classes)]
  if args.imdbval_name != "Stanford_Dog":
    all_boxes_n = [[[] for _ in range(num_images_n)]
                for _ in range(imdb_n.num_classes)]

  output_dir = get_output_dir(imdb_s, save_name)
  dataset_s = roibatchLoader(roidb_s, ratio_list_s, ratio_index_s, 1, \
                            imdb_s.num_classes, is_ws = False, training=False, normalize = False)
  dataloader_s = torch.utils.data.DataLoader(dataset_s, batch_size=1,
                            shuffle=False, num_workers=0,)
  if args.active_learning:
    dataset_ws = roibatchLoader(roidb_ws, ratio_list_ws, ratio_index_ws, 1, \
                              imdb_ws.num_classes, is_ws = True, training=False, normalize = False)
    dataloader_ws = torch.utils.data.DataLoader(dataset_ws, batch_size=1,
                              shuffle=False, num_workers=0, pin_memory=True)
  if args.imdbval_name != "Stanford_Dog":
    dataset_n = roibatchLoader(roidb_n, ratio_list_n, ratio_index_n, 1, \
                              imdb_n.num_classes, is_ws = False, training=False, normalize = False)
    dataloader_n = torch.utils.data.DataLoader(dataset_n, batch_size=1,
                              shuffle=False, num_workers=0, pin_memory=True)

  det_file = os.path.join(output_dir, 'detections.pkl')

  fasterRCNN.eval()
  empty_array = np.transpose(np.array([[],[],[],[],[]]), (1,0)) # (5,0) -> (0,5)

  data_iter = iter(dataloader_s)
  for i in range(num_images_s):

    data = next(data_iter)
    with torch.no_grad():
      im_data.resize_(data[0].size()).copy_(data[0])
      im_info.resize_(data[1].size()).copy_(data[1])
      gt_boxes.resize_(data[2].size()).copy_(data[2])
      num_boxes.resize_(data[3].size()).copy_(data[3])
      im_label.resize_(data[4].size()).copy_(data[4])

    det_tic = time.time()
    rois, cls_prob, bbox_pred, \
    rpn_loss_cls, rpn_loss_box, \
    RCNN_loss_cls, RCNN_loss_bbox, \
    rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes, im_label)

    scores = cls_prob.data
    boxes = rois.data[:, :, 1:5] #(batch, rois, 4)

    if cfg.TEST.BBOX_REG:
      # Apply bounding-box regression deltas
      box_deltas = bbox_pred.data# (batch, rois,4)
      if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
        # Optionally normalize targets by a precomputed mean and stdev
        if args.class_agnostic:
          box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                    + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
          box_deltas = box_deltas.view(1, -1, 4)
        else:
          box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                      + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
          box_deltas = box_deltas.view(1, -1, 4 * len(imdb_s.classes)) # (1, batch*rois, 4*cls)

      pred_boxes = bbox_transform_inv(boxes, box_deltas, 1) # now in form (x,y,x,y)*cls
      pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
    else:
      # Simply repeat the boxes, once for each class
      _ = torch.from_numpy(np.tile(boxes, (1, scores.shape[1])))
      pred_boxes = _.cuda() if args.cuda > 0 else _

    pred_boxes /= data[1][0][2].item() # im_info[0][2] is a scaling info, might need different indexing

    scores = scores.squeeze() # (rois, num_class)
    pred_boxes = pred_boxes.squeeze() # (rois, 4*cls)
    det_toc = time.time()
    detect_time = det_toc - det_tic
    misc_tic = time.time()
    if vis:
      im = cv2.imread(imdb_s.image_path_at(i))
      im2show = np.copy(im)
    for j in range(1, imdb_s.num_classes):
      inds = torch.nonzero(scores[:,j]>thresh).view(-1)
      cls_dets = torch.Tensor([[1.,1.,1.,1.,0]])
      # if there is det
      if inds.numel() > 0:
        cls_scores = scores[:,j][inds] # (rois,) jth cls prob of rois
        _, order = torch.sort(cls_scores, 0, True)
        if args.class_agnostic:
          cls_boxes = pred_boxes[inds, :]
        else:
          cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]

        cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1) # (rois, 5)
        # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
        cls_dets = cls_dets[order]
        keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS) 
        cls_dets = cls_dets[keep.view(-1).long()] # (rois',5)
        all_boxes[j][i] = cls_dets.cpu().numpy()
      else:
        all_boxes[j][i] = empty_array
      if vis:
          gt_boxes_rescale = gt_boxes[:,:5] / im_info.data[0][2]
          gt_boxes_rescale[:,4] *= im_info.data[0][2] # restore class label
          im2show = vis_detections(im2show, imdb_s.classes[j], cls_dets.cpu().numpy(),thresh = cfg.TEST.VIS_THRESH, \
            gt_box = gt_boxes_rescale.cpu().numpy())

    # Limit to max_per_image detections *over all classes*
    if max_per_image > 0:
      image_scores = np.hstack([all_boxes[j][i][:, -1]
                                for j in range(1, imdb_s.num_classes)]) # (rois, num_classes)
      if len(image_scores) > max_per_image:
        image_thresh = np.sort(image_scores)[-max_per_image]
        for j in range(1, imdb_s.num_classes): 
          keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
          all_boxes[j][i] = all_boxes[j][i][keep, :]

    misc_toc = time.time()
    nms_time = misc_toc - misc_tic

    sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
                    .format(i + 1, num_images_s, detect_time, nms_time))
    sys.stdout.flush()
    # if vis and i%10==0:
    if vis:
      cv2.imwrite(output_dir + '/result_test_{}.png'.format(i), im2show)

  #-------------------SAME PROCEDURE FOR test_normal-----------------#     

  if args.imdbval_name != "Stanford_Dog":
    data_iter = iter(dataloader_n)
    for i in range(num_images_n):

      data = next(data_iter)
      with torch.no_grad():
        im_data.resize_(data[0].size()).copy_(data[0])
        im_info.resize_(data[1].size()).copy_(data[1])
        gt_boxes.resize_(data[2].size()).copy_(data[2])
        num_boxes.resize_(data[3].size()).copy_(data[3])
        im_label.resize_(data[4].size()).copy_(data[4])

      det_tic = time.time()
      rois, cls_prob, bbox_pred, \
      rpn_loss_cls, rpn_loss_box, \
      RCNN_loss_cls, RCNN_loss_bbox, \
      rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes, im_label)

      scores = cls_prob.data
      boxes = rois.data[:, :, 1:5] #(batch, rois, 4)

      if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = bbox_pred.data# (batch, rois,4)
        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
          # Optionally normalize targets by a precomputed mean and stdev
          if args.class_agnostic:
            box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                      + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            box_deltas = box_deltas.view(1, -1, 4)
          else:
            box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                        + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            box_deltas = box_deltas.view(1, -1, 4 * len(imdb_n.classes)) # (1, batch*rois, 4*cls)

        pred_boxes = bbox_transform_inv(boxes, box_deltas, 1) # now in form (x,y,x,y)*cls
        pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
      else:
        # Simply repeat the boxes, once for each class
        _ = torch.from_numpy(np.tile(boxes, (1, scores.shape[1])))
        pred_boxes = _.cuda() if args.cuda > 0 else _

      pred_boxes /= data[1][0][2].item() # im_info[0][2] is a scaling info, might need different indexing

      scores = scores.squeeze() # (rois, num_class)
      pred_boxes = pred_boxes.squeeze() # (rois, 4*cls)
      det_toc = time.time()
      detect_time = det_toc - det_tic
      misc_tic = time.time()
      if vis:
        im = cv2.imread(imdb_n.image_path_at(i))
        im2show = np.copy(im)
      for j in range(1, imdb_n.num_classes):
        inds = torch.nonzero(scores[:,j]>thresh).view(-1)
        # if there is det
        if inds.numel() > 0:
          cls_scores = scores[:,j][inds] # (rois,) jth cls prob of rois
          _, order = torch.sort(cls_scores, 0, True)
          if args.class_agnostic:
            cls_boxes = pred_boxes[inds, :]
          else:
            cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]
            # (rois, 4)
            
          cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1) # (rois, 5)
          # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
          cls_dets = cls_dets[order]
          keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS) 
          cls_dets = cls_dets[keep.view(-1).long()] # (rois',5)
          if vis:
            im2show = vis_detections(im2show, imdb_n.classes[j], cls_dets.cpu().numpy(), cfg.TEST.VIS_THRESH)
          all_boxes_n[j][i] = cls_dets.cpu().numpy()
        else:
          all_boxes_n[j][i] = empty_array

      # Limit to max_per_image detections *over all classes*
      if max_per_image > 0:
        image_scores = np.hstack([all_boxes_n[j][i][:, -1]
                                  for j in range(1, imdb_n.num_classes)]) # (rois, num_classes)
        if len(image_scores) > max_per_image:
          image_thresh = np.sort(image_scores)[-max_per_image]
          for j in range(1, imdb_n.num_classes):
            keep = np.where(all_boxes_n[j][i][:, -1] >= image_thresh)[0]
            all_boxes_n[j][i] = all_boxes_n[j][i][keep, :]

      misc_toc = time.time()
      nms_time = misc_toc - misc_tic

      sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
                      .format(i + 1, num_images_n, detect_time, nms_time))
      sys.stdout.flush()

      # if vis and i%10 ==0:
      #   cv2.imwrite(output_dir + '/result_test_normal_{}.png'.format(i), im2show)

  if args.imdbval_name == "Stanford_Dog":
    all_boxes_n = []
  print('Evaluating detections')
  imdb_s.evaluate_detections(all_boxes, all_boxes_n, output_dir, thresh = cfg.TEST.EVAL_OBJ_THRESH)

  with open(det_file, 'wb') as f:
    pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

  end = time.time()
  print("test time: %0.4fs" % (end - start))

  #-------------------ACTIVE LEARNING PHASE-----------------#  
  if args.active_learning: 
    start = time.time()
    data_iter = iter(dataloader_ws)
    for i in range(num_images_ws):

      data = next(data_iter)
      with torch.no_grad():
        im_data.resize_(data[0].size()).copy_(data[0])
        im_info.resize_(data[1].size()).copy_(data[1])
        gt_boxes.resize_(data[2].size()).copy_(data[2])
        num_boxes.resize_(data[3].size()).copy_(data[3])
        im_label.resize_(data[4].size()).copy_(data[4])

      det_tic = time.time()
      rois, cls_prob, bbox_pred, \
      rpn_loss_cls, rpn_loss_box, \
      RCNN_loss_cls, RCNN_loss_bbox, \
      rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes, im_label)

      scores = cls_prob.data
      boxes = rois.data[:, :, 1:5] #(batch, rois, 4)

      if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = bbox_pred.data# (batch, rois,4)
        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
          # Optionally normalize targets by a precomputed mean and stdev
          if args.class_agnostic:
            box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                      + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            box_deltas = box_deltas.view(1, -1, 4)
          else:
            box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                        + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            box_deltas = box_deltas.view(1, -1, 4 * len(imdb_ws.classes)) # (1, batch*rois, 4*cls)

        pred_boxes = bbox_transform_inv(boxes, box_deltas, 1) # now in form (x,y,x,y)*cls
        pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
      else:
        # Simply repeat the boxes, once for each class
        _ = torch.from_numpy(np.tile(boxes, (1, scores.shape[1])))
        pred_boxes = _.cuda() if args.cuda > 0 else _

      pred_boxes /= data[1][0][2].item() # im_info[0][2] is a scaling info, might need different indexing

      scores = scores.squeeze() # (rois, num_class)
      pred_boxes = pred_boxes.squeeze() # (rois, 4*cls)
      detect_time = time.time() - det_tic
      misc_tic = time.time()
      for j in range(1, imdb_ws.num_classes):
        inds = torch.nonzero(scores[:,j]>thresh).view(-1)
        # if there is det
        if inds.numel() > 0:
          cls_scores = scores[:,j][inds] # (rois,) jth cls prob of rois
          _, order = torch.sort(cls_scores, 0, True)
          if args.class_agnostic:
            cls_boxes = pred_boxes[inds, :]
          else:
            cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]
            # (rois, 4)
          cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1) # (rois, 5)
          # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
          cls_dets = cls_dets[order]
          keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS) 
          cls_dets = cls_dets[keep.view(-1).long()] # (rois',5)
          all_boxes_ws[j][i] = cls_dets.cpu().numpy()
        else:
          all_boxes_ws[j][i] = empty_array

      # Limit to max_per_image detections *over all classes*
      if max_per_image > 0:
        image_scores = np.hstack([all_boxes_ws[j][i][:, -1]
                                  for j in range(1, imdb_ws.num_classes)]) # (rois, num_classes)
        if len(image_scores) > max_per_image:
          image_thresh = np.sort(image_scores)[-max_per_image]
          for j in range(1, imdb_ws.num_classes):
            keep = np.where(all_boxes_ws[j][i][:, -1] >= image_thresh)[0]
            all_boxes_ws[j][i] = all_boxes_ws[j][i][keep, :]

      nms_time = time.time() - misc_tic
      sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
                      .format(i + 1, num_images_ws, detect_time, nms_time))
      sys.stdout.flush()
    imdb_ws.active_learning(all_boxes_ws, thresh=cfg.TEST.ACTIVE_LEARNING_THRESH, prob = args.active_learning)

    end = time.time()
    print("\nactive learning time: %0.4fs" % (end - start))
