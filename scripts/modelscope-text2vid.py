# See https://github.com/modelscope/modelscope/tree/master/modelscope/pipelines/multi_modal

from modules import script_callbacks
from types import SimpleNamespace
import gradio as gr
import torch
import random
from pkg_resources import resource_filename
import modules.paths as ph
from modules import lowvram, devices, sd_hijack, shared
import gc
from modules.shared import opts, cmd_opts, state
from scripts.t2v_pipeline import TextToVideoSynthesis, tensor2vid
from scripts.error_hardcode import get_error
from webui import wrap_gradio_gpu_call
import cv2
from base64 import b64encode
import os
import subprocess
import time
from modules import devices
from PIL import Image
import numpy as np
import glob
from tqdm import tqdm

outdir = os.path.join(opts.outdir_img2img_samples, 'text2video-modelscope')
outdir = os.path.join(os.getcwd(), outdir)

pipe = None

def setup_pipeline():
    return TextToVideoSynthesis(ph.models_path+'/ModelScope/t2v')

i1_store_t2v = f"<p style=\"text-align:center;font-weight:bold;margin-bottom:0em\">ModelScope text2video extension for auto1111 — version 1.0b. The video will be shown below this label when ready</p>"

welcome_text = '''Put your models from <a style="color:SteelBlue" href="https://huggingface.co/damo-vilab/modelscope-damo-text-to-video-synthesis/tree/main">https://huggingface.co/damo-vilab/modelscope-damo-text-to-video-synthesis/tree/main</a> to stable-diffusion-webui/models/ModelScope/t2v/. Make sure, you downloaded the file named 'configuration.json' in its raw text form (click on the ⬇️ character to the right, don't save via right-click).

8gbs of VRAM on top of SD should be enough to launch (when the VAE unloading will be fixed, before that orient around ~12 gbs).

Join the development or report issues and feature requests here <a style="color:SteelBlue" href="https://github.com/deforum-art/sd-webui-modelscope-text2video">https://github.com/deforum-art/sd-webui-modelscope-text2video</a>

<italic>If you liked this extension, please <a style="color:SteelBlue" href="https://github.com/deforum-art/sd-webui-modelscope-text2video">give it a star on GitHub</a>!</italic> 😊

'''


def process(skip_video_creation, ffmpeg_location, ffmpeg_crf, ffmpeg_preset, fps, add_soundtrack, soundtrack_path, prompt, n_prompt, steps, frames, cfg_scale, width, height, eta,\
             prompt_v, n_prompt_v, steps_v, frames_v, cfg_scale_v, width_v, height_v, eta_v, \
                cpu_vae='GPU (half precision)', keep_pipe=False,
                do_img2img=False, img2img_frames=None, img2img_steps=0,img2img_startFrame=0
            ):
    global pipe
    print(f"\033[4;33mModelScope text2video extension for auto1111 webui\033[0m")
    print(f"Git commit: {get_t2v_version()}")
    global i1_store_t2v
    outdir_current = os.path.join(outdir, f"{time.strftime('%Y%m%d%H%M%S')}")
    dataurl = get_error()
    try:
        if shared.sd_model is not None:
            sd_hijack.model_hijack.undo_hijack(shared.sd_model)
            try:
                lowvram.send_everything_to_cpu()
            except e:
                ...
            del shared.sd_model
            shared.sd_model = None
        gc.collect()
        devices.torch_gc()

        print('Starting text2video')
        print('Pipeline setup')

        # optionally store pipe in global between runs
        if pipe is None:
            pipe = setup_pipeline()

        device=devices.get_optimal_device()
        print('device',device)

        if do_img2img:

            prompt, n_prompt, steps, frames, cfg_scale, width, height, eta = prompt_v, n_prompt_v, steps_v, frames_v, cfg_scale_v, width_v, height_v, eta_v

            if img2img_frames is None:
                raise FileNotFoundError("Please upload a video :()")

            print("got a request to *vid2vid* an existing video.")

            in_vid_fps, _, _ = get_quick_vid_info(img2img_frames.name)
            folder_name = clean_folder_name(Path(img2img_frames.name).stem)
            outdir_no_tmp = os.path.join(os.getcwd(), 'outputs', 'frame-vid2vid', folder_name)
            i = 1
            while os.path.exists(outdir_no_tmp):
                outdir_no_tmp = os.path.join(os.getcwd(), 'outputs', 'frame-vid2vid', folder_name + '_' + str(i))
                i += 1

            outdir_v2v = os.path.join(outdir_no_tmp, 'tmp_input_frames')
            os.makedirs(outdir, exist_ok=True)
            
            vid2frames(video_path=img2img_frames.name, video_in_frame_path=outdir_v2v, overwrite=True, extract_from_frame=img2img_startFrame, extract_to_frame=img2img_startFrame+frames, numeric_files_output=True, out_img_format='png')
            
            temp_convert_raw_png_path = os.path.join(raw_output_imgs_path, "tmp_vid2vid_folder")
            duplicate_pngs_from_folder(raw_output_imgs_path, temp_convert_raw_png_path, img_batch_id, orig_vid_name)

            videogen = []
            for f in os.listdir(temp_convert_raw_png_path):
                # double check for old _depth_ files, not really needed probably but keeping it for now
                if '_depth_' not in f:
                    videogen.append(f)
                    
            videogen.sort(key= lambda x:int(x.split('.')[0]))

            images=[]
            for file in tqdm(videogen, desc="Loading frames"):
                image=Image.open(file)
                image=image.resize((width,height), Image.ANTIALIAS)
                array = np.array(image)
                images+=[array]

            #print(images)

            images=np.stack(images)# f h w c
            batches=1
            n_images=np.tile(images[np.newaxis, ...], (batches, 1, 1, 1, 1)) # n f h w c
            bcfhw=n_images.transpose(0,4,1,2,3)
            #convert to 0-1 float
            bcfhw=bcfhw.astype(np.float32)/255
            bfchw=bcfhw.transpose(0,2,1,3,4)#b c f h w

            print(f"Converted the frames to tensor {bfchw.shape}")

            vd_out=torch.from_numpy(bcfhw).to("cuda")

            #should be -1,1, not 0,1
            vd_out=2*vd_out-1

            #latents should have shape num_sample, 4, max_frames, latent_h,latent_w
            print("Computing latents")
            latents = pipe.compute_latents(vd_out).to(device)
        else:
            latents = None
            img2img_steps=0

        print('Starting text2video' if not do_img2img else 'Starting video2video')

        samples, _ = pipe.infer(prompt, n_prompt, steps, frames, cfg_scale,
                                width, height, eta, cpu_vae, device, latents,skip_steps=img2img_steps)

        print(f'text2video finished, saving frames to {outdir_current}')

        # just deleted the folder so we need to make it again
        os.makedirs(outdir_current, exist_ok=True)
        for i in range(len(samples)):
            cv2.imwrite(outdir_current + os.path.sep +
                        f"{i:06}.png", samples[i])

        # TODO: add params to the GUI
        if not skip_video_creation:
            ffmpeg_stitch_video(ffmpeg_location=ffmpeg_location, fps=fps, outmp4_path=outdir_current + os.path.sep + f"vid.mp4", imgs_path=os.path.join(outdir_current,
                                "%06d.png"), stitch_from_frame=0, stitch_to_frame=-1, add_soundtrack=add_soundtrack, audio_path=soundtrack_path, crf=ffmpeg_crf, preset=ffmpeg_preset)
        print(f't2v complete, result saved at {outdir_current}')

        mp4 = open(outdir_current + os.path.sep + f"vid.mp4", 'rb').read()
        dataurl = "data:video/mp4;base64," + b64encode(mp4).decode()
    except Exception as e:
        print('Exception occured')
        print(e)
    finally:
        #optionally store pipe in global between runs, if not, remove it
        if not keep_pipe:
            pipe = None
        devices.torch_gc()
        gc.collect()
        devices.torch_gc()
        i1_store_t2v = f'<p style=\"font-weight:bold;margin-bottom:0em\">ModelScope text2video extension for auto1111 — version 1.0b </p><video controls loop><source src="{dataurl}" type="video/mp4"></video>'
    return f'Video at {outdir_current} ready!'

def setup_common_values():
    with gr.Row():
        prompt = gr.Text(
            label='Prompt', max_lines=1, interactive=True)
    with gr.Row():
        n_prompt = gr.Text(label='Negative prompt', max_lines=1,
                            interactive=True, value='text, watermark, copyright, blurry')
    with gr.Row():
        steps = gr.Slider(
            label='Steps',
            minimum=1,
            maximum=100,
            step=1,
            value=30,
            info='Steps')
        cfg_scale = gr.Slider(
            label='cfg_scale',
            minimum=1,
            maximum=100,
            step=1,
            value=12.5,
            info='Steps')
    with gr.Row():
        frames = gr.Slider(
            label="frames", value=24, minimum=2, maximum=125, step=1, interactive=True, precision=0)
        seed = gr.Slider(
            label='Seed',
            minimum=-1,
            maximum=1000000,
            step=1,
            value=-1,
            info='If set to -1, a different seed will be used each time.')
    with gr.Row():
        width = gr.Slider(
            label='width',
            minimum=64,
            maximum=1024,
            step=64,
            value=256,
            info='If set to -1, a different seed will be used each time.')
        height = gr.Slider(
            label='height',
            minimum=64,
            maximum=1024,
            step=64,
            value=256,
            info='If set to -1, a different seed will be used each time.')
    with gr.Row():
        eta = gr.Number(
            label="eta", value=0, interactive=True)
    
    return prompt, n_prompt, steps, frames, cfg_scale, width, height, eta

def on_ui_tabs():
    global i1_store_t2v
    # Uses only SD-requirements + ffmpeg
    dv = SimpleNamespace(**DeforumOutputArgs())
    with gr.Blocks(analytics_enabled=False) as deforum_interface:
        with gr.Row(elem_id='t2v-core').style(equal_height=False, variant='compact'):
            with gr.Column(scale=1, variant='panel'):
                with gr.Tabs():
                    do_img2img = gr.State(value=0)
                    with gr.Tab('txt2vid') as tab_txt2vid:
                        # TODO: make it how it's done in Deforum/WebUI, so we won't have to track individual vars
                        prompt, n_prompt, steps, frames, cfg_scale, width, height, eta = setup_common_values()

                    with gr.Tab('vid2vid') as tab_vid2vid:
                        img2img_frames = gr.File(label="Input video", interactive=True, file_count="single", file_types=["video"], elem_id="vid_to_vid_chosen_file")
                        # TODO: here too
                        prompt_v, n_prompt_v, steps_v, frames_v, cfg_scale_v, width_v, height_v, eta_v = setup_common_values()
                        with gr.Row():
                            img2img_steps = gr.Slider(
                                label="img2img steps", value=dv.img2img_steps, minimum=0, maximum=100, step=1)
                            img2img_startFrame=gr.Number(label='vid2vid start frame',value=dv.img2img_startFrame)
                    
                    tab_txt2vid.select(fn=lambda: 0, inputs=[], outputs=[do_img2img])
                    tab_vid2vid.select(fn=lambda: 1, inputs=[], outputs=[do_img2img])

                    with gr.Tab('Output settings'):
                        with gr.Row(variant='compact') as fps_out_format_row:
                            fps = gr.Slider(label="FPS", value=dv.fps, minimum=1, maximum=240, step=1)
                        with gr.Row(variant='compact') as soundtrack_row:
                            add_soundtrack = gr.Radio(
                                ['None', 'File', 'Init Video'], label="Add soundtrack", value=dv.add_soundtrack)
                            soundtrack_path = gr.Textbox(
                                label="Soundtrack path", lines=1, interactive=True, value=dv.soundtrack_path)

                        with gr.Row(variant='compact'):
                            skip_video_creation = gr.Checkbox(label="Skip video creation", value=dv.skip_video_creation, interactive=True)
                        with gr.Row(equal_height=True, variant='compact', visible=True) as ffmpeg_set_row:
                            ffmpeg_crf = gr.Slider(minimum=0, maximum=51, step=1, label="CRF", value=dv.ffmpeg_crf, interactive=True)
                            ffmpeg_preset = gr.Dropdown(label="Preset", choices=['veryslow', 'slower', 'slow', 'medium', 'fast', 'faster', 'veryfast', 'superfast', 'ultrafast'], interactive=True, value=dv.ffmpeg_preset, type="value")
                        with gr.Row(equal_height=True, variant='compact', visible=True) as ffmpeg_location_row:
                            ffmpeg_location = gr.Textbox(label="Location", lines=1, interactive=True, value=dv.ffmpeg_location)
                    with gr.Tab('How to install? Where to get help, how to help?'):
                        gr.Markdown(welcome_text)
                
                with gr.Row():
                    cpu_vae = gr.Radio(label='VAE Mode', value='GPU (half precision)', choices=[
                                        'GPU (half precision)', 'GPU', 'CPU (Low VRAM)'], interactive=True)
                with gr.Row():
                    keep_pipe = gr.Checkbox(
                        label="keep pipe in memory", value=False, interactive=True)
            with gr.Column(scale=1, variant='compact'):
                with gr.Row():
                    run_button = gr.Button('Generate', variant='primary')
                with gr.Row():
                    i1 = gr.HTML(i1_store_t2v, elem_id='deforum_header')
                with gr.Row(visible=False):
                    result = gr.Label("")
                    result2 = gr.Label("")
                with gr.Row(variant='compact'):
                    btn = gr.Button("Click here after the generation to show the video")
                with gr.Row(variant='compact'):
                    i1 = gr.HTML(i1_store_t2v, elem_id='deforum_header')
                    # Show video

                    def show_vid():
                        return {
                            i1: gr.update(value=i1_store_t2v, visible=True),
                            btn: gr.update(value="Update the video", visible=True),
                        }

                    btn.click(
                        show_vid,
                        [],
                        [i1, btn],
                    )
            dummy_component = gr.Label(visible=False)
            run_button.click(
                # , extra_outputs=[None, '', '']),
                fn=wrap_gradio_gpu_call(process),
                # _js="submit_deforum",
                inputs=[skip_video_creation, ffmpeg_location, ffmpeg_crf, ffmpeg_preset, fps, add_soundtrack, soundtrack_path,
                        prompt, n_prompt, steps, frames, cfg_scale, width, height, eta,\
                        prompt_v, n_prompt_v, steps_v, frames_v, cfg_scale_v, width_v, height_v, eta_v,\
                        cpu_vae, keep_pipe,
                        do_img2img, img2img_frames, img2img_steps,img2img_startFrame
                        ],  # [dummy_component, dummy_component] +
                outputs=[
                    result, result2,
                ],
            )

    return [(deforum_interface, "ModelScope text2video", "t2v_interface")]

script_callbacks.on_ui_tabs(on_ui_tabs)

def find_ffmpeg_binary():
    try:
        import google.colab
        return 'ffmpeg'
    except:
        pass
    for package in ['imageio_ffmpeg', 'imageio-ffmpeg']:
        try:
            package_path = resource_filename(package, 'binaries')
            files = [os.path.join(package_path, f) for f in os.listdir(
                package_path) if f.startswith("ffmpeg-")]
            files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            return files[0] if files else 'ffmpeg'
        except:
            return 'ffmpeg'

def get_t2v_version():
    from modules import extensions as mext
    try:
        for ext in mext.extensions:
            if ext.name in ["sd-webui-modelscope-text2video"] and ext.enabled:
                return ext.version
        return "Unknown"
    except:
        return "Unknown"

def DeforumOutputArgs():
    skip_video_creation = False
    fps = 15
    make_gif = False
    delete_imgs = False  # True will delete all imgs after a successful mp4 creation
    image_path = "C:/SD/20230124234916_%09d.png"
    mp4_path = "testvidmanualsettings.mp4"
    ffmpeg_location = find_ffmpeg_binary()
    ffmpeg_crf = '17'
    ffmpeg_preset = 'slow'
    add_soundtrack = 'None'  # ["File","Init Video"]
    soundtrack_path = "https://deforum.github.io/a1/A1.mp3"
    # End-Run upscaling
    r_upscale_video = False
    r_upscale_factor = 'x2'  # ['2x', 'x3', 'x4']
    # 'realesr-animevideov3' (default of realesrgan engine, does 2-4x), the rest do only 4x: 'realesrgan-x4plus', 'realesrgan-x4plus-anime'
    r_upscale_model = 'realesr-animevideov3'
    r_upscale_keep_imgs = True

    render_steps = False
    path_name_modifier = "x0_pred"  # ["x0_pred","x"]
    store_frames_in_ram = False
    # **Interpolate Video Settings**
    frame_interpolation_engine = "None"  # ["None", "RIFE v4.6", "FILM"]
    frame_interpolation_x_amount = 2  # [2 to 1000 depends on the engine]
    frame_interpolation_slow_mo_enabled = False
    frame_interpolation_slow_mo_amount = 2  # [2 to 10]
    frame_interpolation_keep_imgs = False
    keep_pipe_in_memory = False
    do_img2img = False
    img2img_steps = 15
    img2img_startFrame=0
    return locals()


# Stitch images to a h264 mp4 video using ffmpeg
def ffmpeg_stitch_video(ffmpeg_location=None, fps=None, outmp4_path=None, stitch_from_frame=0, stitch_to_frame=None, imgs_path=None, add_soundtrack=None, audio_path=None, crf=17, preset='veryslow'):
    start_time = time.time()

    print(f"Got a request to stitch frames to video using FFmpeg.\nFrames:\n{imgs_path}\nTo Video:\n{outmp4_path}")
    msg_to_print = f"Stitching *video*..."
    print(msg_to_print)
    if stitch_to_frame == -1:
        stitch_to_frame = 999999999
    try:
        cmd = [
            ffmpeg_location,
            '-y',
            '-vcodec', 'png',
            '-r', str(float(fps)),
            '-start_number', str(stitch_from_frame),
            '-i', imgs_path,
            '-frames:v', str(stitch_to_frame),
            '-c:v', 'libx264',
            '-vf',
            f'fps={float(fps)}',
            '-pix_fmt', 'yuv420p',
            '-crf', str(crf),
            '-preset', preset,
            '-pattern_type', 'sequence',
            outmp4_path
        ]
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
    except FileNotFoundError:
        print("\r" + " " * len(msg_to_print), end="", flush=True)
        print(f"\r{msg_to_print}", flush=True)
        raise FileNotFoundError(
            "FFmpeg not found. Please make sure you have a working ffmpeg path under 'ffmpeg_location' parameter.")
    except Exception as e:
        print("\r" + " " * len(msg_to_print), end="", flush=True)
        print(f"\r{msg_to_print}", flush=True)
        raise Exception(
            f'Error stitching frames to video. Actual runtime error:{e}')

    if add_soundtrack != 'None':
        audio_add_start_time = time.time()
        try:
            cmd = [
                ffmpeg_location,
                '-i',
                outmp4_path,
                '-i',
                audio_path,
                '-map', '0:v',
                '-map', '1:a',
                '-c:v', 'copy',
                '-shortest',
                outmp4_path+'.temp.mp4'
            ]
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            if process.returncode != 0:
                print("\r" + " " * len(msg_to_print), end="", flush=True)
                print(f"\r{msg_to_print}", flush=True)
                raise RuntimeError(stderr)
            os.replace(outmp4_path+'.temp.mp4', outmp4_path)
            print("\r" + " " * len(msg_to_print), end="", flush=True)
            print(f"\r{msg_to_print}", flush=True)
            print(f"\rFFmpeg Video+Audio stitching \033[0;32mdone\033[0m in {time.time() - start_time:.2f} seconds!", flush=True)
        except Exception as e:
            print("\r" + " " * len(msg_to_print), end="", flush=True)
            print(f"\r{msg_to_print}", flush=True)
            print(f'\rError adding audio to video. Actual error: {e}', flush=True)
            print(f"FFMPEG Video (sorry, no audio) stitching \033[33mdone\033[0m in {time.time() - start_time:.2f} seconds!", flush=True)
    else:
        print("\r" + " " * len(msg_to_print), end="", flush=True)
        print(f"\r{msg_to_print}", flush=True)
        print(f"\rVideo stitching \033[0;32mdone\033[0m in {time.time() - start_time:.2f} seconds!", flush=True)

# TODO: MOVEME
# quick-retreive frame count, FPS and H/W dimensions of a video (local or URL-based)
def get_quick_vid_info(vid_path):
    vidcap = cv2.VideoCapture(vid_path)
    video_fps = vidcap.get(cv2.CAP_PROP_FPS)
    video_frame_count = int(vidcap.get(cv2.CAP_PROP_FRAME_COUNT)) 
    video_width = int(vidcap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_height = int(vidcap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    vidcap.release()
    if video_fps.is_integer():
        video_fps = int(video_fps)

    return video_fps, video_frame_count, (video_width, video_height)
