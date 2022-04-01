#!/usr/bin/python3
import os
import argparse
import time
from rivuletpy.trace import R2Tracer
from rivuletpy.utils.io import loadimg, crop, swc2world, swc2vtk
from scipy.ndimage.interpolation import zoom
import SimpleITK as sitk
from rivuletpy.trace import estimate_radius


def show_logo():
    s = "====================Welcome to Rivulet2=================================="
    s += """\n\n8888888b.  d8b                   888          888           .d8888b.  
888   Y88b Y8P                   888          888          d88P  Y88b 
888    888                       888          888                 888 
888   d88P 888 888  888 888  888 888  .d88b.  888888            .d88P 
8888888P\"  888 888  888 888  888 888 d8P  Y8b 888           .od888P\"  
888 T88b   888 Y88  88P 888  888 888 88888888 888          d88P\"      
888  T88b  888  Y8bd8P  Y88b 888 888 Y8b.     Y88b.        888\"       
888   T88b 888   Y88P    \"Y88888 888  \"Y8888   \"Y888       888888888\n"""
    print(s)


def main(file=None, out=None, threshold=0, zoom_factor=1, save_soma=False,
         speed=False, quality=False, clean=True, non_stop=False, npush=0,
         silent=False, skeletonize=False, view=False, tracing_resolution=1.0, vtk=False, slicer=False):

    starttime = time.time()
    img = loadimg(file, tracing_resolution)
    imgdtype = img.dtype
    imgshape = img.shape

    if not silent:
        print('The shape of the image is', img.shape)
    # Modify the crop function so that it can crop somamask as well
    img, crop_region = crop(img, threshold)  # Crop by default

    if zoom_factor != 1.:
        if not silent:
            print('-- Zooming image to %.2f of original size' %
                  zoom_factor)
        img = zoom(img, zoom_factor)

    # Run rivulet2 for the first time
    tracer = R2Tracer(quality=quality,
                      silent=silent,
                      speed=speed,
                      clean=clean,
                      non_stop=non_stop,
                      skeletonize=skeletonize)
    swc, soma = tracer.trace(img, threshold)
    print('-- Finished: %.2f sec.' % (time.time() - starttime))

    # if skeletonized, re-estimate the radius for each node
    if skeletonize:
        print('Re-estimating radius...')
        swc_arr = swc.get_array()
        for i in range(swc_arr.shape[0]):
            swc_arr[i, 5] = estimate_radius(swc_arr[i, 2:5], img > threshold)
        swc._data = swc_arr

    if npush > 0:
        swc.push_nodes_with_binary(img > threshold, niter=npush)
    swc.reset(crop_region, zoom_factor)
    outpath = out if out else os.path.splitext(file)[
                                  0] + '.r2.swc'

    swc.save(outpath)

    # Save the soma mask if required
    if save_soma:
        soma.pad(crop_region, imgshape)
        soma.save((os.path.splitext(outpath)[0] + '.soma.tif'))

    # Save to vtk is required
    if vtk:
        print('Saving to SWC format...')
        swc.save(outpath.replace('.vtk', '.swc'))

        if not file.endswith('.tif'):
            print('Converting to world space...')
            img = sitk.ReadImage(file)
            swcarr = swc2world(swc.get_array(),
                               img,
                               [tracing_resolution] * 3,
                               slicer=slicer)
            swc._data[:, :7] = swcarr
        print('Saving to VTK format...')
        swc2vtk(swc, outpath.replace('.swc', '.vtk'))

    if view:
        from rivuletpy.utils.io import loadswc
        from rivuletpy.swc import SWC
        if os.path.exists(outpath):
            s = SWC()
            s._data = loadswc(outpath)
            s.view()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Arguments to perform the Rivulet2 tracing algorithm.')
    parser.add_argument(
        '-f',
        '--file',
        type=str,
        default=None,
        required=True,
        help='The input file. A image file (*.tif, *.nii, *.mat).')
    parser.add_argument(
        '-o',
        '--out',
        type=str,
        default=None,
        required=False,
        help='The name of the output file')
    parser.add_argument(
        '-t',
        '--threshold',
        type=float,
        default=0,
        help='threshold to distinguish the foreground and background. Default 0. If threshold<0, otsu will be used.'
    )
    parser.add_argument(
        '-z',
        '--zoom_factor',
        type=float,
        default=1.,
        help='The factor to zoom the image to speed up the whole thing. Default 1.')

    # Arguments for soma detection
    parser.add_argument('--save-soma', dest='save_soma', action='store_true',
                        help="Save the automatically reconstructed soma volume along with the SWC.")
    parser.add_argument('--no-save-soma', dest='save_soma', action='store_false',
                        help="Don't save the automatically reconstructed soma volume along with the SWC (default)")
    parser.set_defaults(save_soma=False)

    # Args for tracing
    parser.add_argument('--speed', dest='speed', action='store_true',
                        help="Use the input directly as speed image")
    parser.set_defaults(speed=False)

    parser.add_argument('--quality', dest='quality', action='store_true',
                        help="Reconstruct the neuron with higher quality and slightly more computing time")
    parser.add_argument('--no-quality', dest='quality', action='store_false',
                        help="Reconstruct the neuron with lower quality and slightly more computing time")
    parser.set_defaults(quality=False)

    parser.add_argument('--clean', dest='clean', action='store_true',
                        help="Remove the unconnected segments (default). It is relatively safe to do with the "
                             "Rivulet2 algorithm")
    parser.add_argument('--no-clean', dest='clean', action='store_false',
                        help="Keep the unconnected segments")
    parser.set_defaults(clean=True)

    parser.add_argument('--non-stop', dest='non_stop', action='store_true',
                        help="Whether to ignore the stopping criteria & just keep going no matter what. This is only "
                             "used when you are sure all the positive voxels should belong to the structures of "
                             "interests.")
    parser.set_defaults(non_stop=False)

    parser.add_argument('--npush', dest='npush', type=int,
                        default=0,
                        help="Number of iterations for pushing the centerline nodes to the center with the binary map "
                             "boundaries. This is a post-processing step not nessensary for all the applications. "
                             "When the number of steps increases, each node will be closer to the center locally and "
                             "the curves will be less smoother.")

    # MISC
    parser.add_argument('--silent', dest='silent',
                        action='store_true', help="Omit the terminal outputs")
    parser.add_argument('--no-silent', dest='silent', action='store_false',
                        help="Show the terminal outputs & the nice logo (default)")
    parser.set_defaults(silent=False)
    parser.add_argument('--skeletonize', dest='skeletonize',
                        action='store_true',
                        help="Preprocessing with skelontonization on the segmentation first before tracing. "
                             "Recommended for structures with large diameter variance.",
                        default=False)

    parser.add_argument('-v', '--view', dest='view', action='store_true',
                        help="View the reconstructed neuron when rtrace finishes")
    parser.add_argument('--no-view', dest='view', action='store_false')
    parser.set_defaults(view=False)

    parser.add_argument('--tracing_resolution', type=float, required=False,
                        help='Only valid for input files compatible with ITK. Will resample the image array into '
                             'isotropic resolution before tracing. Default 1mm',
                        default=1.)
    parser.add_argument('--vtk', action='store_true',
                        help="Store the world coordinate vtk format along with the swc", default=False)
    parser.add_argument('--slicer', action='store_true',
                        help="Whether to save vtk coordinates in RAS space that can be rendered in 3D Slicer",
                        default=False)

    args = parser.parse_args()

    if not args.silent:
        show_logo()

    main(file=args.file, out=args.out, threshold=args.threshold, zoom_factor=args.zoom_factor,
         save_soma=args.save_soma, speed=args.speed, quality=args.quality, clean=args.clean,
         non_stop=args.non_stop, npush=args.npush, silent=args.silent, skeletonize=args.skeletonize,
         view=args.view, tracing_resolution=args.tracing_resolution, vtk=args.vtk, slicer=args.slicer)
