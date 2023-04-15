# The API and the UI should be fully autonomus
import sys, os
basedirs = [os.getcwd()]
if 'google.colab' in sys.modules:
    basedirs.append('/content/gdrive/MyDrive/sd/stable-diffusion-webui') #hardcode as TheLastBen's colab seems to be the primal source

for basedir in basedirs:
    deforum_paths_to_ensure = [basedir + '/extensions/sd-webui-text2video/scripts', basedir + '/extensions/sd-webui-modelscope-text2video/scripts', basedir]

    for deforum_scripts_path_fix in deforum_paths_to_ensure:
        if not deforum_scripts_path_fix in sys.path:
            sys.path.extend([deforum_scripts_path_fix])

import hashlib
import io
import json
import logging
import os
import sys
import tempfile
from PIL import Image
import urllib
from typing import Union
import traceback
from types import SimpleNamespace

from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from t2v_helpers.video_audio_utils import find_ffmpeg_binary
from t2v_helpers.general_utils import get_t2v_version
from t2v_helpers.args import T2VArgs_sanity_check, T2VArgs, T2VOutputArgs
from t2v_helpers.render import run
import uuid

logger = logging.getLogger(__name__)

current_directory = os.path.dirname(os.path.abspath(__file__))
if current_directory not in sys.path:
    sys.path.append(current_directory)

def t2v_api(_, app: FastAPI):
    logger.debug(f"text2video extension for auto1111 webui")
    logger.debug(f"Git commit: {get_t2v_version()}")
    logger.debug("Loading text2video API endpoints")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": exc.errors(), "body": exc.body}),
        )
    
    @app.get("/t2v/api_version")
    async def t2v_api_version():
        return JSONResponse(content={"version": '0.1b'})
    
    @app.get("/t2v/version")
    async def t2v_version():
        return JSONResponse(content={"version": get_t2v_version()})

    @app.post("/t2v/run")
    async def t2v_run(prompt: str, n_prompt: Union[str, None] = None, steps: Union[int, None] = None, frames: Union[int, None] = None, seed: Union[int, None] = None, \
                      cfg_scale: Union[int, None] = None, width: Union[int, None] = None, height: Union[int, None] = None, eta: Union[float, None] = None, batch_count: Union[int, None] = None, \
                      do_img2img:bool = False, vid2vid_input: UploadFile | None = None,strength: Union[float, None] = None,img2img_startFrame: Union[int, None] = None, \
                      inpainting_image: UploadFile | None = None, inpainting_frames: Union[int, None] = None, inpainting_weights: Union[str, None] = None,):
        for basedir in basedirs:
            sys.path.extend([
                basedir + '/scripts',
                basedir + '/extensions/sd-webui-text2video/scripts',
                basedir + '/extensions/sd-webui-modelscope-text2video/scripts',
            ])
        
        args_dict = locals()
        default_args_dict = T2VArgs()
        for k, v in args_dict.items():
            if v is None and k in default_args_dict:
                args_dict[k] = default_args_dict[k]

        """
        Run t2v over api
        @return:
        """
        d = SimpleNamespace(**args_dict)
        dv = SimpleNamespace(**T2VOutputArgs())

        # Wrap the process call in a try-except block to handle potential errors
        try:
            T2VArgs_sanity_check(d)

            videodat = run(
                # ffmpeg params
                dv.skip_video_creation, #skip_video_creation
                find_ffmpeg_binary(), #ffmpeg_location
                dv.ffmpeg_crf, #ffmpeg_crf
                dv.ffmpeg_preset,#ffmpeg_preset
                dv.fps,#fps
                dv.add_soundtrack,#add_soundtrack
                dv.soundtrack_path,#soundtrack_paths

                d.prompt,#prompt
                d.n_prompt,#n_prompt
                d.steps,#steps
                d.frames,#frames
                d.seed,#seed
                d.cfg_scale,#cfg_scale
                d.width,#width
                d.height,#height
                d.eta,#eta
                d.batch_count,#batch_count

                # The same, but for vid2vid. Will deduplicate later
                d.prompt,#prompt
                d.n_prompt,#n_prompt
                d.steps,#steps
                d.frames,#frames
                d.seed,#seed
                d.cfg_scale,#cfg_scale
                d.width,#width
                d.height,#height
                d.eta,#eta
                d.batch_count,#batch_count_v

                do_img2img,#do_img2img
                vid2vid_input,#img2img_frames
                "",#img2img_frames_path
                d.strength,#strength
                d.img2img_startFrame,#img2img_startFrame
                inpainting_image,#inpainting_image
                d.inpainting_frames,#inpainting_frames
                d.inpainting_weights,#inpainting_weights
                "ModelScope",#model_type. Only one has stable support at this moment
            )

            return JSONResponse(content={"mp4s": videodat})
        except Exception as e:
            # Log the error and return a JSON response with an appropriate status code and error message
            logger.error(f"Error processing the video: {e}")
            traceback.print_exc()
            return JSONResponse(
                status_code=500,
                content={"detail": "An error occurred while processing the video."},
            )


try:
    import modules.script_callbacks as script_callbacks

    script_callbacks.on_app_started(t2v_api)
    logger.debug("SD-Webui text2video API layer loaded")
except ImportError:
    logger.debug("Unable to import script callbacks.XXX")
