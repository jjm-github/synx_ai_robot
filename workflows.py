"""ComfyUI API workflows distilled from the production experiments in this chat."""
from __future__ import annotations

from typing import Any


def _loaders(cfg: dict[str, Any], kind: str) -> dict[str, str]:
    m = cfg.get("comfy", {}).get("models", {})
    if kind == "flux3":
        return {
            "unet": m.get("unet_flux3", "flux3-image-klein-int8-convrot.safetensors"),
            "clip": m.get("clip_flux3", "qwen3vl_4b_int8_convrot.safetensors"),
            "vae": m.get("vae_flux3", "flux2-vae.safetensors"),
        }
    return {
        "unet": m.get("unet_wan22", "Wan2_2-TI2V-5B_fp8_e4m3fn_scaled_KJ.safetensors"),
        "clip": m.get("clip_wan", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
        "vae": m.get("vae_wan", "wan2.2_vae.safetensors"),
    }


def flux3_i2v(prompt: str, negative: str, image: str | list[str], width: int, height: int,
              seed: int, cfg: dict[str, Any], steps: int = 8) -> dict[str, Any]:
    """FLUX.3 Klein image edit/restyle, as used for watercolor/ink frames."""
    l = _loaders(cfg, "flux3")
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": l["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": l["clip"], "type": "flux3", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": l["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "LoadImage", "inputs": {"image": image[0] if isinstance(image, list) else image}},
        "7": {"class_type": "ModelSamplingFlux",
              "inputs": {"model": ["1", 0], "max_shift": 2.77259, "base_shift": 2.77259,
                         "width": width, "height": height}},
        "8": {"class_type": "ReferenceMultiLatent",
              "inputs": {"positive": ["4", 0], "negative": ["5", 0], "vae": ["3", 0],
                         "resolution": "resize", "resolution.megapixels": 1.7,
                         "images.image0": ["6", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["7", 0], "positive": ["8", 0], "negative": ["8", 1],
                         "latent_image": ["8", 2], "seed": seed, "steps": steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "robot_flux3"}},
    }
    extra = image if isinstance(image, list) else []
    for i, name in enumerate(extra[1:], start=1):
        lid = str(20 + i * 2)
        wf[lid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["8"]["inputs"][f"images.image{i}"] = [lid, 0]
    return wf


def flux3_t2i(prompt: str, negative: str, width: int, height: int,
              seed: int, cfg: dict[str, Any], steps: int = 24) -> dict[str, Any]:
    l = _loaders(cfg, "flux3")
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": l["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": l["clip"], "type": "flux3", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": l["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": width, "height": height}},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "9": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "10": {"class_type": "CFGGuider",
               "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "cfg": 2.0}},
        "11": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["8", 0], "guider": ["10", 0], "sampler": ["9", 0],
                          "sigmas": ["7", 0], "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0], "filename_prefix": "robot_flux3"}},
    }


def wan22_i2v(prompt: str, negative: str, image: str, width: int, height: int,
              length: int, seed: int, cfg: dict[str, Any],
              steps: int = 20) -> dict[str, Any]:
    l = _loaders(cfg, "wan22")
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": l["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": l["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": l["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": width, "height": height,
                         "length": length, "batch_size": 1, "start_image": ["8", 0]}},
        "8": {"class_type": "LoadImage", "inputs": {"image": image}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "seed": seed, "steps": steps, "cfg": 5.0,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24.0}},
        "12": {"class_type": "SaveVideo",
               "inputs": {"video": ["11", 0], "filename_prefix": "video/robot_wan22",
                          "format": "mp4", "codec": "auto"}},
    }


def wan22_t2v(prompt: str, negative: str, width: int, height: int,
              length: int, seed: int, cfg: dict[str, Any],
              steps: int = 20) -> dict[str, Any]:
    l = _loaders(cfg, "wan22")
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": l["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": l["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": l["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}},
        "7": {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": width, "height": height,
                         "length": length, "batch_size": 1}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "seed": seed, "steps": steps, "cfg": 5.0,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "positive": ["5", 0], "negative": ["6", 0],
                         "latent_image": ["7", 0], "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24.0}},
        "12": {"class_type": "SaveVideo",
               "inputs": {"video": ["11", 0], "filename_prefix": "video/robot_wan22",
                          "format": "mp4", "codec": "auto"}},
    }


def build_for_task(task: dict[str, Any], cfg: dict[str, Any], comfy_image: str | None) -> dict[str, Any]:
    """Pick a workflow based on task naming/prompt, using only known-good nodes."""
    text = " ".join(str(task.get(k) or "") for k in ("task_name", "task_type", "prompt")).lower()
    wants_video = any(k in text for k in ("wan", "i2v", "t2v", "animate", "motion", "видео", "video"))
    seed = int((task.get("source_json") or {}).get("seed") or 20261117)
    width = int((task.get("source_json") or {}).get("width") or 2048)
    height = int((task.get("source_json") or {}).get("height") or 864)
    frames = int((task.get("source_json") or {}).get("frames") or 144)
    if wants_video and not comfy_image:
        return wan22_t2v(task.get("prompt") or "", task.get("negative") or "",
                         width, height, frames, seed, cfg)
    if wants_video and comfy_image:
        return wan22_i2v(task.get("prompt") or "", task.get("negative") or "",
                         comfy_image, width, height, frames, seed, cfg)
    if comfy_image:
        return flux3_i2v(task.get("prompt") or "", task.get("negative") or "",
                         comfy_image, width, height, seed, cfg)
    return flux3_t2i(task.get("prompt") or "", task.get("negative") or "",
                     width, height, seed, cfg)
