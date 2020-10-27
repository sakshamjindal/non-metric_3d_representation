import numpy as np
import pandas as pd
import os 
import torch
import pickle
import collections, os, io
from PIL import Image
import torch
from torchvision.transforms import ToTensor, Resize
from torch.utils.data import Dataset
import random
import pickle
import utils_disco
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
import ipdb 
st = ipdb.set_trace

def collate_boxes(data):
    query_image, num_boxes_q, boxes_q, query_viewpoint, key_image, num_boxes_k, boxes_k, key_viewpoint, scene_num, key_img_view, pix_T_cams_raw, camR_T_origin_raw, origin_T_camXs_raw = zip(*data)
    batch_size = len(num_boxes_q)
    
#     print(torch.stack(list(query_image)))
    query_image = torch.stack(list(query_image))
    key_image = torch.stack(list(key_image))
    
 
    num_boxes_q = torch.tensor(list(num_boxes_q))
    num_boxes_k = torch.tensor(list(num_boxes_k))
    
    boxes_q = list(boxes_q)
    boxes_k = list(boxes_k)
 
    object_boxes_q = np.concatenate(boxes_q, axis=0)
    object_boxes_k = np.concatenate(boxes_k, axis=0)
    
    query_viewpoint = torch.stack(list(query_viewpoint))
    key_viewpoint = torch.stack(list(key_viewpoint))
    
    scene_num = (list(scene_num))
    key_img_view = (list(key_img_view))
    pix_T_cams_raw = torch.stack(list(pix_T_cams_raw), dim=0)
    camR_T_origin_raw = torch.stack(list(camR_T_origin_raw), axis=0)
    origin_T_camXs_raw = torch.stack(list(origin_T_camXs_raw), axis=0)
    
    
    metadata = {"scene_number":scene_num, "key_image_index":key_img_view, "pix_T_cams_raw":torch.tensor(pix_T_cams_raw).cuda(), "camR_T_origin_raw":torch.tensor(camR_T_origin_raw).cuda(), "origin_T_camXs_raw":torch.tensor(origin_T_camXs_raw).cuda()}
    feed_dict_q = {"images":query_image, "objects":num_boxes_q, "objects_boxes":torch.tensor(object_boxes_q).cuda(), "viewpoint":query_viewpoint}
    feed_dict_k = {"images":key_image, "objects":num_boxes_k, "objects_boxes":torch.tensor(object_boxes_k).cuda(), "viewpoint":key_viewpoint}
    
    
    return feed_dict_q, feed_dict_k, metadata



class GQNDataset_pdisco(Dataset):
    def __init__(self, root_dir, transform=None, target_transform=None, few_shot=False):
        self.root_dir = root_dir
        self.transform = transform
        self.target_transform = target_transform
        self.target_res = 256
        self.N = 10
        self.few_shot = few_shot
        self.views = 40

        if root_dir.endswith("txt"):
            data  = []

            with open(root_dir) as f:
                lines = f.readlines()

                for line in lines:
                    data.append(line.split()[0])

            self.all_files = [os.path.join(os.path.dirname(root_dir),f) for f in data if f.endswith('.p')]
        else:
            self.all_files = [os.path.join(root_dir,f) for f in os.listdir(root_dir) if f.endswith('.p')]
            
        print('Initialised.....',len(self.all_files)*(self.views-1),' files...')
            
    def bbox_rearrange(self,tree,boxes= [],classes={},all_classes=[]):
        for i in range(0, tree.num_children):
            updated_tree,boxes,classes,all_classes = self.bbox_rearrange(tree.children[i],boxes=boxes,classes=classes,all_classes=all_classes)
            tree.children[i] = updated_tree     
        if tree.function == "describe":
            xmax,ymax,zmin,xmin,ymin,zmax = tree.bbox_origin
            box = np.array([xmin,ymin,zmin,xmax,ymax,zmax])
            tree.bbox_origin = box
            boxes.append(box)
            classes["shape"] = tree.word
            all_classes.append(classes)
            classes = {}
        if tree.function == "combine":
            if "large" in tree.word or "small" in tree.word:
                classes["size"] = tree.word
            elif "metal" in tree.word or "rubber" in tree.word:
                classes["material"] = tree.word
            else:
                classes["color"] = tree.word
        return tree,boxes,classes,all_classes

    def __len__(self):
        return len(self.all_files)*(self.views-1)

    def __getitem__(self, idx, is_pickle=True):
        
        #print(idx)

        scene_num = idx // (self.views-1)
        key_img_view = (idx % (self.views-1)) + 1

        scene_path = self.all_files[scene_num]
        data = pickle.load(open(scene_path, "rb"))
        
        viewpoints = torch.tensor(data['origin_T_camXs_raw'])
        
        rx, ry, rz = utils_disco.rotm2eul(viewpoints)
        rx, ry, rz = rx.unsqueeze(1), ry.unsqueeze(1), rz.unsqueeze(1)
        xyz = viewpoints[:, :3, -1]

                
        view_vector = [xyz, torch.cos(rx), torch.sin(rx), torch.cos(rz), torch.sin(rz)]
        viewpoints = torch.cat(view_vector, dim=-1)
        
        images = torch.tensor(data['rgb_camXs_raw']).permute(0,3,1,2)/255.
        _, _, H_orig, W_orig = images.shape
#         if not self.few_shot:
#             images = F.interpolate(images, self.target_res)

        images = images.permute(0,2,3,1)
        query_image, key_image = images[0], images[key_img_view]
        query_viewpoint, key_viewpoint = viewpoints[0], viewpoints[key_img_view]
        
        ###########################
        ## TO FIX - FOR ONE VIEW ##
#         tree_file = pickle.load(open(os.path.join("/home/mprabhud/dataset/clevr_veggies",data['tree_seq_filename']),"rb"))
#         tree,boxes,_,_ = bbox_rearrange(tree_file,boxes=[],classes={},all_classes=[])
#         boxes = np.stack(boxes)
        num_boxes = np.random.randint(low=1, high=5) #boxes.shape
        boxes = np.zeros([num_boxes, 6])
        ##########################
        
        pix_T_cams_raw = np.stack((data['pix_T_cams_raw'][0], data['pix_T_cams_raw'][key_img_view]))
        # print("Pixt camXs shape: ", pix_T_cams_raw.shape)
        if not self.few_shot:
            pix_T_cams_raw = utils_disco.scale_intrinsics(torch.tensor(pix_T_cams_raw), self.target_res/(1.*W_orig), self.target_res/(1.*H_orig))
        
        
        camR_T_origin_raw = np.stack((data['camR_T_origin_raw'][0], data['camR_T_origin_raw'][key_img_view]))
        origin_T_camXs_raw = np.stack((data['origin_T_camXs_raw'][0], data['origin_T_camXs_raw'][key_img_view]))
        
        
#         metadata = {"scene_number":scene_num, "key_image_index":key_img_view, "pix_T_cams_raw":torch.tensor(pix_T_cams_raw).cuda(), "camR_T_origin_raw":torch.tensor(camR_T_origin_raw).cuda(), "origin_T_camXs_raw":torch.tensor(origin_T_camXs_raw).cuda()}
#         feed_dict_q = {"images":query_image, "objects":num_boxes, "objects_boxes":torch.tensor(boxes).cuda(), "viewpoint":query_viewpoint}
#         feed_dict_k = {"images":key_image, "objects":num_boxes, "objects_boxes":torch.tensor(boxes).cuda(), "viewpoint":key_viewpoint}
        return torch.tensor(query_image), num_boxes, torch.tensor(boxes), torch.tensor(query_viewpoint), torch.tensor(key_image), num_boxes, torch.tensor(boxes), torch.tensor(key_viewpoint), scene_num, key_img_view, torch.tensor(pix_T_cams_raw), torch.tensor(camR_T_origin_raw), torch.tensor(origin_T_camXs_raw)





if __name__ == '__main__':
	
	train_dataset = GQNDataset_pdisco(root_dir='/home/mprabhud/dataset/clevr_veggies/npys/be_lt.txt')
	from torch.utils.data import DataLoader
	train_loader = DataLoader(train_dataset, batch_size=5, shuffle=True, collate_fn=collate_boxes)

	for b in train_loader:
	    query_image, key_image, query_viewpoint, key_viewpoint, metadata = b
	    break

	f, axarr = plt.subplots(5,2)
	for row in range(5):
	    axarr[row,0].imshow(query_image[row])
	    axarr[row,1].imshow(key_image[row])


