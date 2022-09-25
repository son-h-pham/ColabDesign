import jax
import jax.numpy as jnp
import numpy as np

from colabdesign.shared.utils import copy_dict
from colabdesign.af.alphafold.model import config

def setup(self, crop_len=128, crop_mode="slide", crop_iter=5):

  if hasattr(self,"set_crop"):
    self.set_crop(crop_len, crop_mode, crop_iter)

  else:
    def set_crop(crop_len=128, crop_mode="slide", crop_iter=5):
      assert crop_mode in ["slide","roll","pair"]
      assert crop_len < sum(self._lengths)
      self._args["crop"] = {"len":crop_len, "mode":crop_mode, "iter":crop_iter}

    self.set_crop = set_crop
    self.set_crop(crop_len, crop_mode, crop_iter)

    # add distances
    if self.protocol == "fixbb":
      cb_atoms = self._pdb["cb_feat"]["atoms"]
      cb_atoms[self._pdb["cb_feat"]["mask"] == 0,:] = np.nan
      cb_dist = np.sqrt(np.square(cb_atoms[:,None] - cb_atoms[None,:]).sum(-1))
      self._cmap = cb_dist < self.opt["cmap_cutoff"]

    # function to apply to inputs
    def pre_callback(inputs, opt):
      '''crop features'''
      p = opt["crop_pos"]
      inputs.update(crop_feat(inputs, p))

    # function to apply to outputs
    def post_callback(aux, opt):
      '''uncrop features'''
      length = sum(self._lengths)
      def uncrop_feat(x, pos, pair=False):
        if pair:
          p1, p2 = pos[:,None], pos[None,:]
          return jnp.zeros((length,length)+x.shape[2:]).at[p1,p2].set(x)
        else:
          return jnp.zeros((length,)+x.shape[1:]).at[pos].set(x)
      p,x = opt["crop_pos"], aux["prev"]
      full_aux = {"prev":{
          "prev_pos":  uncrop_feat(x["prev_pos"],  p),
          "prev_pair": uncrop_feat(x["prev_pair"], p, pair=True),
          "prev_msa_first_row": uncrop_feat(x["prev_msa_first_row"], p)
        },
      }
      aux.update(full_aux)

    # function to apply during design
    def design_callback(self):

      def check():
        if self._args["crop"]["len"] >= sum(self._lengths): return False
        if self._args["copies"] > 1 and not self._args["repeat"]: return False
        if self.protocol in ["partial","binder"]: return False
        return True

      def update_aux(accumulate=False):
        # uncrop/accumulate features
        L = sum(self._lengths)
        def uncrop_feat(x, pos, pair=False):
          if pair:
            y = np.full((L,L)+x.shape[2:],np.nan)
            y[pos[:,None],pos[None,:]] = x
          else:
            y = np.full((L,)+x.shape[1:],np.nan)
            y[pos] = x
          return y        
        
        # uncrop features
        a, p = self.aux, self.opt["crop_pos"]
        vs = {k:uncrop_feat(a[k],p,pair=True) for k in ["cmap","pae"]}
        vs.update({k:uncrop_feat(a[k],p) for k in ["plddt","atom_positions"]})          
        
        if accumulate:
          # accumulate features
          for k,v in vs.items():
            w = self._tmp["crop"].get(k,v)
            w = np.where(np.isnan(w),v,w)
            self._tmp["crop"][k] = np.where(np.isnan(v),w,(v+w)/2)
          self.aux.update(self._tmp["crop"])
        else:
          self.aux.update(vs)
    
      def update_pos():
        c = self._args["crop"]
        if c["mode"] == "slide":
          i = np.random.randint(0,(L-max_L)+1)
          self.opt["crop_pos"] = np.arange(i,i+max_L)      
        if c["mode"] == "roll":
          i = np.random.randint(0,L)
          self.opt["crop_pos"] = np.sort(np.roll(np.arange(L),L-i)[:max_L])
        if c["mode"] == "pair":
          # pick random pair of interactig crops
          max_L = max_L // 2
          # pick first crop
          i_range = np.append(np.arange(0,(L-2*max_L)+1),np.arange(max_L,(L-max_L)+1))
          i = np.random.choice(i_range)          
          # pick second crop
          j_range = np.append(np.arange(0,(i-max_L)+1),np.arange(i+max_L,(L-max_L)+1))
          if hasattr(self,"_cmap"):
            # if contact map defined, bias to interacting pairs
            w = np.array([self._cmap[i:i+max_L,j:j+max_L].sum() for j in j_range]) + 1e-8
            j = np.random.choice(j_range, p=w/w.sum())
          else:
            j = np.random.choice(j_range)               
          self.opt["crop_pos"] = np.sort(np.append(np.arange(i,i+max_L),np.arange(j,j+max_L)))

      if check():
        if "crop" not in self._tmp:
          self._tmp["crop"] = {}
          update_pos()
          self._args["crop"]["k"] = self._k
        
        elif self._k != self._args["crop"]["k"]:
          update_aux(accumulate=True)
          if (self._k % self._args["crop"]["iter"]) == 0:
            update_pos()
          self._args["crop"]["k"] = self._k      
        
        else:
          update_aux()

    #################################################################

    # definite initial crop_pos
    design_callback(self)
    self._opt["crop_pos"] = self.opt["crop_pos"]

    # populate callbacks
    self._callbacks["pre"].append(pre_callback)
    self._callbacks["post"].append(post_callback)
    self._callbacks["design"].append(design_callback)

def crop_feat(feat, pos):  
  '''crop features to specified [pos]itions'''
  if feat is None: return None
  def find(x,k):
    i = []
    for j,y in enumerate(x):
      if y == k: i.append(j)
    return i
  shapes = config.CONFIG.data.eval.feat
  NUM_RES = "num residues placeholder"
  idx = {k:find(v,NUM_RES) for k,v in shapes.items()}
  new_feat = copy_dict(feat)
  for k in new_feat.keys():
    if k in ["batch","prev"]:
      new_feat[k] = crop_feat(feat[k], pos)
    if k in idx:
      for i in idx[k]: new_feat[k] = jnp.take(new_feat[k], pos, i)
  return new_feat