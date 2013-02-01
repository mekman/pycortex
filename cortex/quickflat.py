import os
import sys
import time
import glob
import Image
import cPickle
import cStringIO
import binascii
import numpy as np

import db
import utils

def _gen_flat_mask(subject, height=1024):
    import polyutils
    import Image
    import ImageDraw
    pts, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    left, right = polyutils.trace_both(pts, polys)

    pts -= pts.min(0)
    pts *= height / pts.max(0)[1]

    im = Image.new('L', (int(pts.max(0)[0]), height))
    draw = ImageDraw.Draw(im)
    draw.polygon(pts[left, :2].ravel().tolist(), fill=255)
    draw.polygon(pts[right, :2].ravel().tolist(), fill=255)
    return np.array(im) > 0

def _make_flat_cache(subject, xfmname, height=1024):
    from scipy.spatial import cKDTree

    flat, polys, norm = db.surfs.getVTK(subject, "flat", merge=True, nudge=True)
    valid = np.unique(polys)
    fmax, fmin = flat.max(0), flat.min(0)
    size = fmax - fmin
    aspect = size[0] / size[1]
    width = int(aspect * height)

    mask = _gen_flat_mask(subject, height=height).T
    assert mask.shape[0] == width and mask.shape[1] == height

    grid = np.mgrid[fmin[0]:fmax[0]:width*1j, fmin[1]:fmax[1]:height*1j].reshape(2,-1)
    kdt = cKDTree(flat[valid,:2])
    dist, idx = kdt.query(grid.T[mask.ravel()])

    return valid[idx], mask

cache = dict()
def get_cache(subject, xfmname, recache=False, height=1024):
    key = (subject, xfmname, height)
    if not recache and key in cache:
        return cache[key]

    cacheform = db.surfs.getFiles(subject)['flatcache']
    cachefile = cacheform.format(xfmname=xfmname, height=height, date="*")
    #pull a list of candidate cache files
    files = glob.glob(cachefile)
    if len(files) < 1 or recache:
        #if recaching, delete all existing files
        for f in files:
            os.unlink(f)
        print "Generating a flatmap cache"
        #pull points and transform from database
        verts, mask = _make_flat_cache(subject, xfmname, height=height)
        #save them into the proper file
        date = time.strftime("%Y%m%d")
        cachename = cacheform.format(xfmname=xfmname, height=height, date=date)
        cPickle.dump((verts, mask), open(cachename, "w"), 2)
    else:
        verts, mask = cPickle.load(open(files[0]))

    cache[key] = verts, mask
    return verts, mask

def make(data, subject, xfmname, recache=False, height=1024, projection='nearest', **kwargs):
    mapper = utils.get_mapper(subject, xfmname, type=projection)
    verts, mask = get_cache(subject, xfmname, recache=recache, height=height)

    mdata = np.hstack(mapper(data))
    if mdata.dtype == np.uint8:
        mdata = mdata.swapaxes(-1, -2)
        if mdata.ndim == 2:
            mdata = mdata[np.newaxis]
        shape = (mdata.shape[0],) + mask.shape + (mdata.shape[-1],)
    elif mdata.ndim == 1:
        mdata = mdata[np.newaxis]
        shape = (data.shape[0],) + mask.shape

    img = np.nan*np.ones(shape, dtype=data.dtype)
    img[:, mask] = mdata[:,verts]
    return img.swapaxes(1, 2)[:,::-1].squeeze()

rois = dict()
def overlay_rois(im, subject, name=None, height=1024, labels=True, **kwargs):
    import shlex
    import subprocess as sp
    from matplotlib.pylab import imsave

    if name is None:
        name = 'png:-'

    key = (subject, labels)
    if key not in rois:
        print "loading %s"%subject
        rois[key] = utils.get_roipack(subject).get_texture(height, labels=labels)
    cmd = "composite {rois} - {name}".format(rois=rois[key].name, name=name)
    proc = sp.Popen(shlex.split(cmd), stdin=sp.PIPE, stdout=sp.PIPE)

    fp = cStringIO.StringIO()
    imsave(fp, im, **kwargs)
    fp.seek(0)
    out, err = proc.communicate(fp.read())
    if len(out) > 0:
        fp = cStringIO.StringIO()
        fp.write(out)
        fp.seek(0)
        return fp

def make_figure(im, subject, name=None, with_rois=True, labels=True, colorbar=True, bgcolor=None, dpi=100, **kwargs):
    from matplotlib import pyplot as plt
    fig = plt.figure()
    ax = fig.add_axes((0,0,1,1))
    cimg = ax.imshow(im, aspect='equal', **kwargs)
    ax.axis('off')

    if colorbar:
        cbar = fig.add_axes((.4, .07, .2, .04))
        fig.colorbar(cimg, cax=cbar, orientation='horizontal')

    if with_rois:
        key = (subject, labels)
        if key not in rois:
            roi = utils.get_roipack(subject)
            rois[key] = roi.get_texture(im.shape[0], labels=labels)
        rois[key].seek(0)
        oax = fig.add_axes((0,0,1,1))
        oimg = oax.imshow(plt.imread(rois[key]), aspect='equal', interpolation='bicubic')

    if name is None:
        return fig
    
    fig.set_size_inches(np.array(im.shape[:2])[::-1] / float(dpi))
    if bgcolor is None:
        fig.savefig(name, transparent=True, dpi=dpi)
    else:
        fig.savefig(name, facecolor=bgcolor, transparent=False, dpi=dpi)
    plt.close()

def make_movie(name, data, subject, xfmname, recache=False, height=1024, projection='nearest', dpi=100, tr=2, interp='linear', fps=30, vcodec='libtheora', bitrate="8000k", vmin=None, vmax=None, **kwargs):
    import sys
    import shlex
    import shutil
    import tempfile
    import subprocess as sp
    import multiprocessing as mp
    
    from scipy.interpolate import interp1d

    #make the flatmaps
    ims = make(data, subject, xfmname, recache=recache, height=height, projection=projection)
    if vmin is None:
        vmin = np.nanmin(ims)
    if vmax is None:
        vmax = np.nanmax(ims)

    #Create the matplotlib figure
    fig = make_figure(ims[0], subject, vmin=vmin, vmax=vmax, **kwargs)
    fig.set_size_inches(np.array([ims.shape[2], ims.shape[1]]) / float(dpi))
    img = fig.axes[0].images[0]

    #set up interpolation
    times = np.arange(0, len(ims)*tr, tr)
    interp = interp1d(times, ims, kind=interp, axis=0, copy=False)
    frames = np.linspace(0, times[-1], (len(times)-1)*tr*fps+1)
    
    try:
        path = tempfile.mkdtemp()
        impath = os.path.join(path, "im%09d.png")

        def overlay(idxts):
            idx, ts = idxts
            img.set_data(interp(ts))
            fig.savefig(impath%idx, transparent=True, dpi=dpi)
            print "      \r%.02f%%...."%(float(idx) / len(frames) * 100) ,
            sys.stdout.flush()

        map(overlay, enumerate(frames))

        cmd = "avconv -i {path} -vcodec {vcodec} -r {fps} -b {br} {name}".format(path=impath, vcodec=vcodec, fps=fps, br=bitrate, name=name)
        sp.call(shlex.split(cmd))
    finally:
        shutil.rmtree(path)

def make_png(name, data, subject, xfmname, recache=False, height=1024, projection='nearest', **kwargs):
    im = make(data, subject, xfmname, recache=recache, height=height, projection=projection)
    return make_figure(im, subject, name=name, **kwargs)

def show(data, subject, xfmname, recache=False, height=1024, projection='nearest', **kwargs):
    im = make(data, subject, xfmname, recache=recache, height=height, projection=projection)
    return make_figure(im, subject, **kwargs)
