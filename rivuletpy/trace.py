import os
import math
from random import random
from collections import Counter
import numpy as np
from scipy import ndimage 
from scipy.spatial.distance import cdist
import progressbar
from scipy.interpolate import RegularGridInterpolator 
from skimage.morphology import skeletonize_3d
import skfmm

from .utils.preprocessing import distgradient

def makespeed(dt, threshold=0):
    '''
    Make speed image for FM from distance transform
    '''

    F = dt ** 4
    F[F<=threshold] = 1e-10

    return F


def iterative_backtrack(t, bimg, somapt, somaradius, render=False, silence=False, eraseratio=1.1):
    ''' 
    Trace the segmented image with a single neuron using Rivulet2 algorithm.

    Parameters
    ----------------
    t  :  The time-crossing map generated by fast-marching
    bimg  :  The binary image as 3D numpy ndarray with foreground (True) and background (False)
    somapt  :  The soma position as a 3D coordinate in 3D numpy ndarray
    somaradius  :  The approximate soma radius
    render  :  The flag to render the tracing progress for debugging
    silence  :  The flag to silent the tracing progress without showing the progress bar
    eraseratio  :  The ratio to enlarge the inital surface of the branch erasing
    '''

    config = {'length':6, 'coverage':0.98, 'gap':15}

    # Get the gradient of the Time-crossing map
    dx, dy, dz = distgradient(t.astype('float64'))
    standard_grid = (np.arange(t.shape[0]), np.arange(t.shape[1]), np.arange(t.shape[2]))
    ginterp = (RegularGridInterpolator(standard_grid, dx),
               RegularGridInterpolator(standard_grid, dy),
               RegularGridInterpolator(standard_grid, dz))

    bounds = t.shape
    tt = t.copy()
    tt[bimg <= 0] = -2
    bb = np.zeros(shape=tt.shape) # For making a large tube to contain the last traced branch

    if render:
        from .utils.rendering3 import Viewer3, Line3, Ball3
        viewer = Viewer3(800, 800, 800)
        viewer.set_bounds(0, bounds[0], 0, bounds[1], 0, bounds[2])

    # Start tracing loop
    nforeground = bimg.sum()
    converage = 0.0
    iteridx = 0
    swc = None
    if not silence: bar = progressbar.ProgressBar(max_value=nforeground)
    velocity = None

    while converage < config['coverage']:
        iteridx += 1
        coveredctr = np.logical_and(tt<0, bimg > 0).sum() 
        converage =  coveredctr / nforeground

        # Find the geodesic furthest point on foreground time-crossing-map
        endpt = srcpt = np.asarray(np.unravel_index(tt.argmax(), tt.shape)).astype('float64')
        if not silence: bar.update(coveredctr)

        # Trace it back to maxd 
        branch = [srcpt,]
        reached = False
        touched = False
        notmoving =False 
        valueerror = False 
        fgctr = 0 # Count how many steps are made on foreground in this branch
        steps_after_reach = 0
        outofbound = reachedsoma = False

        # For online confidence comupting
        online_voxsum = 0.
        low_online_conf = False

        line_color = [random(), random(), random()]

        while True: # Start 1 Back-tracking iteration
            try:
                endpt = rk4(srcpt, ginterp, t, 1)
                endptint = [math.floor(p) for p in endpt]
                velocity = endpt - srcpt

                # See if it travels too far on the background
                endpt_b = bimg[endptint[0], endptint[1], endptint[2]]
                fgctr += endpt_b

                # Compute the online confidence
                online_voxsum += endpt_b
                online_confidence = online_voxsum / (len(branch) + 1)

                if np.linalg.norm(somapt - endpt) < 1.2 * somaradius: # Stop due to reaching soma point
                    reachedsoma = True

                    # Render a yellow node at fork point
                    if render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0.917, 0.933, 0.227)
                        viewer.add_geom(ball)
                    break

                # Render the line segment
                if render:
                    l = Line3(srcpt, endpt)
                    l.set_color(*line_color)
                    viewer.add_geom(l)
                    viewer.render(return_rgb_array=False)

                # Consider reaches previous explored area traced with real branch
                # Note: when the area was traced due to noise points (erased with -2), not considered as 'reached'
                if tt[endptint[0], endptint[1], endptint[2]] == -1:  
                    reached = True

                if reached: # If the endpoint reached previously traced area check for node to connect for at each step
                    if swc is None: break # There has not been any branch added yet

                    steps_after_reach += 1
                    endradius = getradius(bimg, endpt[0], endpt[1], endpt[2])
                    touched, touchidx = match(swc, endpt, endradius)
                    closestnode = swc[touchidx, :]

                    if touched or steps_after_reach >= 100: 
                        # Render a blue node at fork point
                        if touched and render:
                            ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                            ball.set_color(0, 0, 1)
                            viewer.add_geom(ball)
                        break

                # If the velocity is too small, sprint a bit with the momentum
                if np.linalg.norm(velocity) <= 0.5 and len(branch) >= config['length']:
                    endpt = srcpt + (branch[-1] - branch[-4])

                if len(branch) > 15 and np.linalg.norm(branch[-15] - endpt) < 1.: 
                    notmoving = True
                    # print('==Not Moving - Velocity:', velocity)
                    # Render a brown node at stopping point since not moving
                    if render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0.729, 0.192, 0.109)
                        viewer.add_geom(ball)
                    break # There could be zero gradients somewhere

                if online_confidence < 0.25:
                    low_online_conf = True

                    # Render a grey node at stopping point with low confidence
                    if render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0.5, 0.5, 0.5)
                        viewer.add_geom(ball)
                    break 

                # All in vain finally if it traces out of bound
                if not inbound(endpt, tt.shape): 
                    outofbound = True
                    break

            except ValueError:
                valueerror = True
                # print('== Value ERR - Velocity:', velocity, 'Point:', endpt)
                # Render a pink node at value error 
                if render:
                    ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                    ball.set_color(0.972, 0.607, 0.619)
                    viewer.add_geom(ball)
                break 

            branch.append(endpt) # Add the newly traced node to current branch
            srcpt = endpt # Shift forward

        # Check forward confidence 
        cf = conf_forward(branch, bimg)

        ## Erase it from the timemap
        rlist = []
        for node in branch:
            n = [math.floor(n) for n in node]
            r = getradius(bimg, n[0], n[1], n[2])
            r = 1 if r < 1 else r
            rlist.append(r)
            
            # To make sure all the foreground voxels are included in bb
            r *= eraseratio
            r = math.ceil(r)
            X, Y, Z = np.meshgrid(constrain_range(n[0]-r, n[0]+r+1, 0, tt.shape[0]),
                                  constrain_range(n[1]-r, n[1]+r+1, 0, tt.shape[1]),
                                  constrain_range(n[2]-r, n[2]+r+1, 0, tt.shape[2]))
            bb[X, Y, Z] = 1

        startidx = [math.floor(p) for p in branch[0]]
        endidx = [math.floor(p) for p in branch[-1]]

        if len(branch) > config['length'] and tt[endidx[0], endidx[1], endidx[2]] < tt[startidx[0], startidx[1], startidx[2]]:
            erase_region = np.logical_and(tt[endidx[0], endidx[1], endidx[2]] <= tt, tt <= tt[startidx[0], startidx[1], startidx[2]])
            erase_region = np.logical_and(bb, erase_region)
        else:
            erase_region = bb.astype('bool')

        if np.count_nonzero(erase_region) > 0:
            tt[erase_region] = -2 if low_online_conf else -1
        bb.fill(0)
            
        if touched:
            connectid = swc[touchidx, 0]
        elif reachedsoma:
            connectid = 0 
        else:
            connectid = None

        if cf[-1] < 0.5 or low_online_conf: # Check the confidence of this branch
            continue 

        swc = add2swc(swc, branch, rlist, connectid)
        if notmoving: swc[-1, 1] = 128 # Some weired colour for unexpected stop
        if valueerror: swc[-1, 1] = 256 # Some weired colour for unexpected stop

    # After all tracing iterations, check all unconnected nodes
    for nodeidx in range(swc.shape[0]):
        if swc[nodeidx, -1]  == -2:
            # Find the closest node in swc, excluding the nodes traced earlier than this node in match
            swc2consider = swc[swc[:, 0] > swc[nodeidx, 0], :]
            connect, minidx = match(swc2consider, 
                                                     swc[nodeidx, 2:5], 3)
            if connect:
                swc[nodeidx, -1] = swc2consider[minidx, 0]

    # Prune short leaves 
    swc = prune_leaves(swc, bimg, config['length'], 0.5)

    # Add soma node to the result swc
    somanode = np.asarray([0, 1, somapt[0], somapt[1], somapt[2], somaradius, -1])
    swc = np.vstack((somanode, swc))

    return swc



def iterative_backtrack_r1(t, bimg, somapt, somaradius, gap=8, wiring=1.5, length=4, render=False, silence=True):
    '''
    Trace the segmented image with a single neuron using Rivulet1 algorithm.
    [1] Liu, Siqi, et al. "Rivulet: 3D Neuron Morphology Tracing with Iterative Back-Tracking." Neuroinformatics (2016): 1-15.
    [2] Zhang, Donghao, et al. "Reconstruction of 3D neuron morphology using Rivulet back-tracking." 
    Biomedical Imaging (ISBI), 2016 IEEE 13th International Symposium on. IEEE, 2016.

    This algorithm is deprecated from the standard Rivulet pipeline since Rivulet2 is more accurate and faster than Rivulet1.
    This routine is kept for algorithmic experiments to see the difference between two version of Rivulet
    '''

    config = {'coverage':0.98}

    # Get the gradient of the Time-crossing map
    dx, dy, dz = distgradient(t.astype('float64'))
    standard_grid = (np.arange(t.shape[0]), np.arange(t.shape[1]), np.arange(t.shape[2]))
    ginterp = (RegularGridInterpolator(standard_grid, dx),
               RegularGridInterpolator(standard_grid, dy),
               RegularGridInterpolator(standard_grid, dz))

    bounds = t.shape
    tt = t.copy()
    tt[bimg <= 0] = -2
    bb = np.zeros(shape=tt.shape) # For making a large tube to contain the last traced branch

    if render:
        from .utils.rendering3 import Viewer3, Line3, Ball3
        viewer = Viewer3(800, 800, 800)
        viewer.set_bounds(0, bounds[0], 0, bounds[1], 0, bounds[2])

    # Start tracing loop
    nforeground = bimg.sum()
    converage = 0.0
    iteridx = 0
    swc = None
    if not silence: bar = progressbar.ProgressBar(max_value=nforeground)
    velocity = None

    while converage < config['coverage']:
        iteridx += 1
        coveredctr = np.logical_and(tt<0, bimg > 0).sum() 
        converage =  coveredctr / nforeground

        # Find the geodesic furthest point on foreground time-crossing-map
        endpt = srcpt = np.asarray(np.unravel_index(tt.argmax(), tt.shape)).astype('float64')
        if not silence: bar.update(coveredctr)

        # Trace it back to maxd 
        branch = [srcpt,]
        reached = False
        touched = False
        notmoving =False 
        valueerror = False 
        gapctr = 0 # Count continous steps on background
        fgctr = 0 # Count how many steps are made on foreground in this branch
        steps_after_reach = 0
        outofbound = reachedsoma = False
        line_color = [random(), random(), random()]

        while True: # Start 1 Back-tracking iteration
            try:
                endpt = rk4(srcpt, ginterp, t, 1)
                endptint = [math.floor(p) for p in endpt]
                velocity = endpt - srcpt

                # See if it travels too far on the background
                endpt_b = bimg[endptint[0], endptint[1], endptint[2]]
                gapctr = 0 if endpt_b else gapctr + 1
                if gapctr > gap: 
                    # print('gap!')
                    break # Stop tracing if gap is too big
                fgctr += endpt_b

                # Compute the online confidence
                # online_voxsum += endpt_b
                # online_confidence = online_voxsum / (len(branch) + 1)

                if np.linalg.norm(somapt - endpt) < 1.2 * somaradius: # Stop due to reaching soma point
                    reachedsoma = True

                    # Render a yellow node at fork point
                    if render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0.917, 0.933, 0.227)
                        viewer.add_geom(ball)
                    break

                # Render the line segment
                if render:
                    l = Line3(srcpt, endpt)
                    l.set_color(*line_color)
                    viewer.add_geom(l)
                    viewer.render(return_rgb_array=False)

                # Consider reaches previous explored area traced with real branch
                # Note: when the area was traced due to noise points (erased with -2), not considered as 'reached'
                if tt[endptint[0], endptint[1], endptint[2]] == -1:  
                    reached = True

                if reached: # If the endpoint reached previously traced area check for node to connect for at each step
                    if swc is None: break # There has not been any branch added yet
                    endradius = getradius(bimg, endpt[0], endpt[1], endpt[2])
                    touched, touchidx = match_r1(swc, endpt, endradius, wiring)
                    closestnode = swc[touchidx, :]

                    if touched and render: # Render a blue node at fork point
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0, 0, 1)
                        viewer.add_geom(ball)
                    break

                # # If the velocity is too small, sprint a bit with the momentum
                if np.linalg.norm(velocity) <= 0.5 and len(branch) >= length:
                    endpt = srcpt + (branch[-1] - branch[-4])

                if len(branch) > 15 and np.linalg.norm(branch[-15] - endpt) < 1.: 
                    notmoving = True
                    # print('==Not Moving - Velocity:', velocity)
                    # Render a brown node at stopping point since not moving
                    if render:
                        ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                        ball.set_color(0.729, 0.192, 0.109)
                        viewer.add_geom(ball)

                    break # There could be zero gradients somewhere

                # All in vain finally if it traces out of bound
                if not inbound(endpt, tt.shape): 
                    outofbound = True
                    break

            except ValueError:
                valueerror = True
                # print('== Value ERR - Velocity:', velocity, 'Point:', endpt)
                # Render a pink node at value error 
                if render:
                    ball = Ball3((endpt[0], endpt[1], endpt[2]), radius=1)
                    ball.set_color(0.972, 0.607, 0.619)
                    viewer.add_geom(ball)
                break 

            branch.append(endpt) # Add the newly traced node to current branch
            srcpt = endpt # Shift forward

        # Check forward confidence 
        # cf = conf_forward(branch, bimg)
        cf = fgctr / len(branch)

        ## Erase it from the timemap
        rlist = []
        for node in branch:
            n = [math.floor(n) for n in node]
            r = getradius(bimg, n[0], n[1], n[2])
            r = 1 if r < 1 else r
            rlist.append(r)
            
            # To make sure all the foreground voxels are included in bb
            r *= 0.8
            r = math.ceil(r)
            X, Y, Z = np.meshgrid(constrain_range(n[0]-r, n[0]+r+1, 0, tt.shape[0]),
                                  constrain_range(n[1]-r, n[1]+r+1, 0, tt.shape[1]),
                                  constrain_range(n[2]-r, n[2]+r+1, 0, tt.shape[2]))
            bb[X, Y, Z] = 1

        erase_region = bb.astype('bool')

        if np.count_nonzero(erase_region) > 0:
            tt[erase_region] = -1
        bb.fill(0)
            
        if touched:
            connectid = swc[touchidx, 0]
        elif reachedsoma:
            connectid = 0 
        else:
            connectid = None

        if cf < 0.3:
            # print('cf low!')
            continue

        # if len(branch) < length: # Check the confidence of this branch
        #     print('length!')
        #     continue 

        swc = add2swc(swc, branch, rlist, connectid)
        if notmoving: swc[-1, 1] = 128 # Some weired colour for unexpected stop
        if valueerror: swc[-1, 1] = 256 # Some weired colour for unexpected stop

    # After all tracing iterations, check all unconnected nodes
    for nodeidx in range(swc.shape[0]):
        if swc[nodeidx, -1]  == -2:
            # Find the closest node in swc, excluding the nodes traced earlier than this node in match
            swc2consider = swc[swc[:, 0] > swc[nodeidx, 0], :]
            connect, minidx = match_r1(swc2consider, swc[nodeidx, 2:5], 3, wiring)
            if connect: swc[nodeidx, -1] = swc2consider[minidx, 0]

    # Prune short leaves 
    swc = prune_leaves(swc, bimg, length, 0.5)

    # Add soma node to the result swc
    somanode = np.asarray([0, 1, somapt[0], somapt[1], somapt[2], somaradius, -1])
    swc = np.vstack((somanode, swc))

    return swc


def gd(srcpt, ginterp, t, stepsize):
    gvec = np.asarray([g(srcpt)[0] for g in ginterp])
    if np.linalg.norm(gvec) <= 0: 
        return np.array([-1, -1, -1])
    gvec /= np.linalg.norm(gvec)
    srcpt -= stepsize * gvec
    return srcpt


def rk4(srcpt, ginterp, t, stepsize):
    # Compute K1
    k1 = np.asarray([g(srcpt)[0] for g in ginterp])
    k1 *= stepsize / max(np.linalg.norm(k1), 1.)
    tp = srcpt - 0.5 * k1 # Position of temporary point
    if not inbound(tp, t.shape):
        return srcpt

    # Compute K2
    k2 = np.asarray([g(tp)[0] for g in ginterp])
    k2 *= stepsize / max(np.linalg.norm(k2), 1.)
    tp = srcpt - 0.5 * k2 # Position of temporary point
    if not inbound(tp, t.shape):
        return srcpt

    # Compute K3
    k3 = np.asarray([g(tp)[0] for g in ginterp])
    k3 *= stepsize / max(np.linalg.norm(k3), 1.)
    tp = srcpt - k3 # Position of temporary point
    if not inbound(tp, t.shape):
        return srcpt

    # Compute K4
    k4 = np.asarray([g(tp)[0] for g in ginterp])
    k4 *= stepsize / max(np.linalg.norm(k4), 1.)

    return srcpt - (k1 + k2*2 + k3*2 + k4)/6.0 # Compute final point


def getradius(bimg, x, y, z):
    r = 0
    x = math.floor(x)   
    y = math.floor(y)   
    z = math.floor(z)   

    while True:
        r += 1
        try:
            if bimg[max(x-r, 0) : min(x+r+1, bimg.shape[0]),
                    max(y-r, 0) : min(y+r+1, bimg.shape[1]), 
                    max(z-r, 0) : min(z+r+1, bimg.shape[2])].sum() / (2*r + 1)**3 < .6:
                break
        except IndexError:
            break

    return r


def inbound(pt, shape):
    return all([True if 0 <= p <= s-1 else False for p,s in zip(pt, shape)])


def fibonacci_sphere(samples=1, randomize=True):
    rnd = 1.
    if randomize:
        rnd = random() * samples

    points = []
    offset = 2./samples
    increment = math.pi * (3. - math.sqrt(5.));

    for i in range(samples):
        y = ((i * offset) - 1) + (offset / 2);
        r = math.sqrt(1 - pow(y,2))

        phi = ((i + rnd) % samples) * increment

        x = math.cos(phi) * r
        z = math.sin(phi) * r

        points.append(np.array([x, y, z]))

    return points


def match_r1(swc, pos, radius, wiring):
    '''
    The node match used by Rivulet1 which uses a wiring threshold
    Deprecated in the standard Rivulet pipeline 
    Used only for experiments
    '''
    # Find the closest ground truth node 
    nodes = swc[:, 2:5]
    distlist = np.squeeze(cdist(pos.reshape(1,3), nodes))
    if distlist.size == 0:
        return False, -2
    minidx = distlist.argmin()
    minnode = swc[minidx, 2:5]

    # See if either of them can cover each other with a ball of their own radius
    mindist = np.linalg.norm(pos - minnode)
    return radius > wiring * mindist or swc[minidx, 5] * wiring > mindist, minidx


def match(swc, pos, radius): 
    # Find the closest ground truth node 
    nodes = swc[:, 2:5]
    distlist = np.squeeze(cdist(pos.reshape(1,3), nodes))
    if distlist.size == 0:
        return False, -2
    minidx = distlist.argmin()
    minnode = swc[minidx, 2:5]

    # See if either of them can cover each other with a ball of their own radius
    mindist = np.linalg.norm(pos - minnode)
    return radius > mindist or swc[minidx, 5] > mindist, minidx


def add2swc(swc, path, radius, connectid = None):  
    newbranch = np.zeros((len(path), 7))
    if swc is None: # It is the first branch to be added
        idstart = 1
    else:
        idstart = swc[:, 0].max() + 1

    for i, p in enumerate(path):
        id = idstart+i
        nodetype = 3 # 3 for basal dendrite; 4 for apical dendrite; However now we cannot differentiate them automatically

        if i == len(path) - 1: # The end of this branch
            pid = -2 if connectid is None else connectid
            if connectid is not None and connectid is not 1 and swc is not None: swc[swc[:, 0]==connectid, 1] = 5 # its connected node is fork point 
        else:
            pid = idstart + i + 1
            if i == 0:
                nodetype = 6 # Endpoint

        newbranch[i] = np.asarray([id, nodetype, p[0], p[1], p[2], radius[i], pid])

    if swc is None:
        swc = newbranch
    else:
        # Check if any tail should be connected to its head
        head = newbranch[0]
        matched, minidx = match(swc, head[2:5], head[5])
        if matched and swc[minidx, -1] is -2: swc[minidx, -1] = head[0]
        swc = np.vstack((swc, newbranch))

    return swc


def constrain_range(min, max, minlimit, maxlimit):
    return list(range(min if min > minlimit else minlimit, max if max < maxlimit else maxlimit))


def get_subtree_nodeids(swc, node):
    subtreeids = np.array([])

    # Find children
    # print('-- Node here:', node)
    chidx = np.argwhere(node[0] == swc[:, 6])

    # Recursion stops when there this node is a leaf with no children, return itself 
    if chidx.size == 0:
        # print('== No Child, returning', node[0])
        return node[0]
    else:
        # print('== Got child')
        # Get the node ids of each children
        for c in chidx:
            subids = get_subtree_nodeids(swc, swc[c, :].squeeze())
            # print('==Trying to append', subtreeids, subids, node[0])
            subtreeids = np.hstack((subtreeids, subids, node[0]))

    # print('==Returning:', subtreeids)
    return subtreeids


class Node(object):
    def __init__(self, id):
        self.__id  = id
        self.__links = set()

    @property
    def id(self):
        return self.__id

    @property
    def links(self):
        return set(self.__links)

    def add_link(self, other):
        self.__links.add(other)
        other.__links.add(self)


# The function to look for connected components.
# https://breakingcode.wordpress.com/2013/04/08/finding-connected-components-in-a-graph/
def connected_components(nodes):

    # List of connected components found. The order is random.
    result = []

    # Make a copy of the set, so we can modify it.
    nodes = set(nodes)

    # Iterate while we still have nodes to process.
    while nodes:

        # Get a random node and remove it from the global set.
        n = nodes.pop()

        # This set will contain the next group of nodes connected to each other.
        group = {n}

        # Build a queue with this node in it.
        queue = [n]

        # Iterate the queue.
        # When it's empty, we finished visiting a group of connected nodes.
        while queue:

            # Consume the next item from the queue.
            n = queue.pop(0)

            # Fetch the neighbors.
            neighbors = n.links

            # Remove the neighbors we already visited.
            neighbors.difference_update(group)

            # Remove the remaining nodes from the global set.
            nodes.difference_update(neighbors)

            # Add them to the group of connected nodes.
            group.update(neighbors)

            # Add them to the queue, so we visit them in the next iterations.
            queue.extend(neighbors)

        # Add the group to the list of groups.
        result.append(group)

    # Return the list of groups.
    return result


def cleanswc(swc, radius=True):
    '''
    Only keep the largest connected component
    '''
    swcdict = {}
    for n in swc: # Hash all the swc nodes
        swcdict[n[0]] = Node(n[0])

    for n in swc: # Add mutual links for all nodes
        id = n[0]
        pid = n[-1]

        if pid >= 1: swcdict[id].add_link(swcdict[pid])

    groups = connected_components(set(swcdict.values()))
    lenlist = [len(g) for g in groups]
    maxidx = lenlist.index(max(lenlist))
    set2keep = groups[maxidx]
    id2keep = [n.id for n in set2keep]
    swc = swc[np.in1d(swc[:, 0], np.asarray(id2keep)), :]
    if not radius:
        swc[:,5] = 1

    return swc


def confidence_cut(swc, img, marginsize=3):
    '''
    DEPRECATED FOR NOW
    Confidence Cut on the leaves
    For each leave,  if the total forward confidence is smaller than 0.5, the leave is dumped 
    For leave with forward confidence > 0.5, find the cut point with the largest difference between
    the forward and backward confidence
    If the difference on the cut point > 0.5, make the cut here
    '''

    id2dump = [] 

    # Find all the leaves
    childctr = Counter(swc[:, -1])
    leafidlist= [id for id in childctr if childctr[id] == 0]

    for leafid in leafidlist: # Iterate each leaf node
        nodeid = leafid 

        branch = []
        while True: # Get the leaf branch out
            node = swc[swc[:, 0] == nodeid, :]
            branch.append(node)
            parentid = node[-1]
            if childctr[parentid] is not 1: break # merged / unconnected
            nodeid = parentid

        # Forward confidence
        conf_forward = np.zeros(shape=(len(branch), ))
        branchvox = np.asarray([ img[math.floor(p[2]), math.floor(p[3]), math.floor(p[4])] for p in branch])
        for i in range(len(branch, )):
            conf_forward[i] = branchvox[:i].sum() / (i+1)

        if conf_forward[-1] < 0.5: # Dump immediately if the forward confidence is too low
            id2dump.extend([b[0] for b in branch])
            print(id2dump)
            continue

        if len(branch) <= 2*marginsize: # The branch is too short for confidence cut, leave it
            continue

        # Backward confidence    
        conf_backward = np.zeros(shape=(len(branch, )))
        for i in range(len(path)):
            conf_backward[i] = branchvox[i:].sum() / (len(path) - i)

        # Find the node with highest confidence disagreement
        confdiff = conf_backward - conf_forward
        confdiff = confdiff[marginsize:-marginsize]

        # A cut is needed 
        if confdiff.max() > 0.5:
            cutidx = confdiff.argmax() + marginsize
            id2dump.extend([b[0] for b in branch[:cutpoint]])
        
    # Only keep the swc nodes not in the dump id list
    cuttedswc = []
    for nodeidx in range(swc.shape[0]):
        if swc[nodeidx, 0] not in id2dump:
            cuttedswc.append(swc[nodeidx, :])

    cuttedswc = np.squeeze(np.dstack(cuttedswc)).T

    return cuttedswc


def prune_leaves(swc, img, length, conf):

    # Find all the leaves
    childctr = Counter(swc[:, -1]) 
    leafidlist= [id for id in swc[:, 0] if id not in swc[:, -1] ] # Does not work
    id2dump = []

    for leafid in leafidlist: # Iterate each leaf node
        nodeid = leafid 
        branch = []
        while True: # Get the leaf branch out
            node = swc[swc[:, 0] == nodeid, :].flatten()
            if node.size == 0:
                break 
            branch.append(node)
            parentid = node[-1]
            if childctr[parentid] is not 1: break # merged / unconnected
            nodeid = parentid

        # Prune if the leave is too short | the confidence of the leave branch is too low
        if len(branch) < length or conf_forward([b[2:5] for b in branch], img)[-1] < conf:
            id2dump.extend([ node[0] for node in branch ] )

    # Only keep the swc nodes not in the dump id list
    cuttedswc = []
    for nodeidx in range(swc.shape[0]):
        if swc[nodeidx, 0] not in id2dump:
            cuttedswc.append(swc[nodeidx, :])

    cuttedswc = np.squeeze(np.dstack(cuttedswc)).T
    return cuttedswc


def conf_forward(path, img):
        conf_forward = np.zeros(shape=(len(path), ))
        branchvox = np.asarray([ img[math.floor(p[0]), math.floor(p[1]), math.floor(p[2])] for p in path])
        for i in range(len(path, )):
            conf_forward[i] = branchvox[:i].sum() / (i+1)

        return conf_forward


