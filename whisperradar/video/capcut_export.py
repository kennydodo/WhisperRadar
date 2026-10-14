"""Port of ImgToVideo.CapCut.CapCutDraftExporter: a CapCut desktop draft folder
(draft_content.json + draft_meta_info.json) from a planned timeline.

Schema verified against CapCut desktop 9.4.0.4015 (draft new_version 185.0.0). The JSON is
written like System.Text.Json does (compact, 1.0 -> 1, \\uXXXX escapes for non-ASCII and
HTML-sensitive characters), ids are fresh GUIDs, so two exports differ only in those.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from . import models as m

STILL_MATERIAL_DURATION_US = 10800000000
DRAFT_NEW_VERSION = "185.0.0"
_ESC = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b",
        "\f": "\\f"}
_HTML = set("<>&'+`")


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _new_lower_id() -> str:
    return uuid.uuid4().hex


def _us(frames: int, fps: float) -> int:
    return int(round(frames * 1_000_000.0 / fps))


def _forward(path: str) -> str:
    return path.replace("\\", "/")


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


# ---- System.Text.Json-style compact serialisation ----------------------------------
def _jstr(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) > 0x7E or ch in _HTML:
            cp = ord(ch)
            if cp > 0xFFFF:
                cp -= 0x10000
                out.append("\\u%04X\\u%04X" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)))
            else:
                out.append("\\u%04X" % cp)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jnum(v) -> str:
    if isinstance(v, int):
        return str(v)
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    r = repr(float(v))
    if "e" in r:
        mant, exp = r.split("e")
        r = f"{mant}E{'+' if int(exp) >= 0 else '-'}{abs(int(exp)):02d}"
    return r


def dumps(node) -> str:
    if node is None:
        return "null"
    if node is True:
        return "true"
    if node is False:
        return "false"
    if isinstance(node, str):
        return _jstr(node)
    if isinstance(node, (int, float)):
        return _jnum(node)
    if isinstance(node, (list, tuple)):
        return "[" + ",".join(dumps(x) for x in node) + "]"
    return "{" + ",".join(_jstr(k) + ":" + dumps(v) for k, v in node.items()) + "}"


# ---- companions --------------------------------------------------------------------
def _clip_block() -> dict:
    return {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}}


def _companion(kind: str, cid: str) -> dict:
    if kind == "canvas":
        return {"id": cid, "type": "canvas_color", "color": "", "blur": 0.0, "image": "",
                "album_image": "", "image_id": "", "image_name": "", "source_platform": 0,
                "team_id": ""}
    if kind == "speed":
        return {"id": cid, "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}
    if kind == "sound":
        return {"id": cid, "type": "", "audio_channel_mapping": 0, "is_config_open": False}
    if kind == "color":
        return {"id": cid, "is_color_clip": False, "is_gradient": False, "solid_color": "",
                "gradient_colors": [], "gradient_percents": [], "gradient_angle": 90.0,
                "width": 0.0, "height": 0.0}
    if kind == "placeholder":
        return {"id": cid, "type": "placeholder_info", "meta_type": "none", "res_path": "",
                "res_text": "", "error_path": "", "error_text": ""}
    return {"id": cid, "type": "vocal_separation", "choice": 0, "removed_sounds": [],
            "time_range": None, "production_path": "", "final_algorithm": "", "enter_from": ""}


_VIDEO_COMPANIONS = (("canvas", "canvases"), ("speed", "speeds"),
                     ("sound", "sound_channel_mappings"), ("color", "material_colors"),
                     ("placeholder", "placeholder_infos"), ("vocal", "vocal_separations"))
_AUDIO_COMPANIONS = (("speed", "speeds"), ("sound", "sound_channel_mappings"))


def _video_material(mid: str, file_path: str, width: int, height: int) -> dict:
    node = json.loads(VIDEO_MATERIAL_TEMPLATE)
    node["id"] = mid
    node["path"] = _forward(file_path)
    node["width"] = width
    node["height"] = height
    node["material_name"] = _basename(file_path)
    return node


def _scale_keyframes(clip, dims, canvas_aspect, duration_us) -> Optional[List[dict]]:
    if clip.motion not in (m.ZOOM_IN, m.ZOOM_OUT):
        return None
    size = dims.get(clip.file_path.lower())
    if not size or size[0] <= 0 or size[1] <= 0:
        return None
    sv, ev = clip.start_viewport, clip.end_viewport
    start_aspect = sv.width / sv.height
    if abs(start_aspect - canvas_aspect) > 0.02:
        return None
    cover_fit = size[1] * canvas_aspect
    s0 = cover_fit / sv.width
    s1 = cover_fit / ev.width
    if clip.motion == m.ZOOM_OUT:
        s0, s1 = max(s0, s1), min(s0, s1)
    else:
        s0, s1 = min(s0, s1), max(s0, s1)

    def keyframe(offset, value):
        return {"id": _new_id(), "left_control": {"x": 0.5, "y": 0.5},
                "right_control": {"x": 0.5, "y": 0.5}, "time_offset": offset, "values": [value]}

    def prop(ptype, *kfs):
        return {"id": _new_id(), "keyframe_list": list(kfs), "material_id": "",
                "property_type": ptype}

    return [prop("KFTypeScaleX", keyframe(0, s0), keyframe(duration_us, s1)),
            prop("KFTypeScaleY", keyframe(0, s0), keyframe(duration_us, s1))]


def build_draft_content(timeline: m.Timeline, images: Sequence[m.ImageInfo],
                        draft_name: str, freezes: Optional[Dict[str, str]] = None) -> str:
    from .. import motion
    max_stretch = motion.MAX_STRETCH_SECONDS

    def motion_max_stretch():
        return max_stretch

    fps = timeline.fps
    cw, ch = timeline.resolution.width, timeline.resolution.height
    canvas_aspect = cw / ch
    clips = [c for s in timeline.scenes for c in s.clips]
    total_us = _us(sum(c.duration_frames for c in clips), fps)
    audio_us = _us(timeline.audio.duration_frames, fps)
    dims = {i.file_path.lower(): (i.width, i.height) for i in images}

    videos: List[dict] = []
    collections: Dict[str, List[dict]] = {k: [] for k in (
        "canvases", "speeds", "sound_channel_mappings", "material_colors",
        "placeholder_infos", "vocal_separations")}

    material_by_file: Dict[str, str] = {}
    freeze_material_by_clip: Dict[int, str] = {}
    for i, clip in enumerate(clips):
        # The animated opening: a clip backed by its AI video uses the mp4 as
        # its material (the video carries its own motion), like the ffmpeg
        # render plan - otherwise CapCut shows the still, not the clip.
        key = (clip.video_path or clip.file_path).lower()
        if key in material_by_file:
            continue
        mid = _new_id()
        material_by_file[key] = mid
        w, h = dims.get(clip.file_path.lower(), (cw, ch))
        videos.append(_video_material(mid, clip.video_path or clip.file_path, w, h))
        # a shot that can outlast its 8s clip freezes the mp4's last frame
        # (the render's tpad clone); the freeze png is its own photo material
        freeze = (freezes or {}).get(clip.video_path) if clip.video_path else None
        if freeze and os.path.isfile(freeze) and clip.duration_frames / fps > motion_max_stretch():
            fmid = _new_id()
            freeze_material_by_clip[i] = fmid
            videos.append(_video_material(fmid, freeze, cw, ch))

    video_segments: List[dict] = []
    for i, clip in enumerate(clips):
        key = (clip.video_path or clip.file_path).lower()
        mid = material_by_file[key]
        dur_us = _us(clip.duration_frames, fps)
        comp_ids = []
        for kind, coll in _VIDEO_COMPANIONS:
            cid = _new_id()
            comp_ids.append(cid)
            collections[coll].append(_companion(kind, cid))
        if clip.video_path:
            # mirror motion.fit like the render: the 8s media plays at fit's
            # speed (covering up to 10s), a longer shot freezes its last frame.
            # Segments must tile the shot's span - anything past the media end
            # is black in CapCut too (the S03_01 black tail).
            from .. import motion
            fit = motion.fit(clip.duration_frames / fps)
            play_us = _us(int(round(fit["play"] * fps)), fps)
            max_stretch_us = _us(int(round(motion.MAX_STRETCH_SECONDS * fps)), fps)
            if dur_us <= max(max_stretch_us, play_us):
                # one segment: slow the media so it covers the whole shot
                video_segments.append({
                    "id": _new_id(), "material_id": mid,
                    "target_timerange": {"start": _us(clip.start_frame, fps),
                                         "duration": dur_us},
                    "source_timerange": {"start": 0, "duration": play_us},
                    "extra_material_refs": comp_ids, "clip": _clip_block(),
                    "common_keyframes": [],
                    "speed": round(play_us / dur_us, 4)})
                continue
            seg = {
                "id": _new_id(), "material_id": mid,
                "target_timerange": {"start": _us(clip.start_frame, fps),
                                     "duration": play_us},
                "source_timerange": {"start": 0, "duration": play_us},
                "extra_material_refs": comp_ids, "clip": _clip_block(),
                "common_keyframes": [],
                "speed": fit["speed"]}
            video_segments.append(seg)
            hold_us = dur_us - play_us
            fmid = freeze_material_by_clip.get(i)
            if hold_us > 0 and fmid:
                comp2 = []
                for kind, coll in _VIDEO_COMPANIONS:
                    cid = _new_id()
                    comp2.append(cid)
                    collections[coll].append(_companion(kind, cid))
                video_segments.append({
                    "id": _new_id(), "material_id": fmid,
                    "target_timerange": {"start": _us(clip.start_frame, fps) + play_us,
                                         "duration": hold_us},
                    "source_timerange": {"start": 0, "duration": hold_us},
                    "extra_material_refs": comp2, "clip": _clip_block(),
                    "common_keyframes": [], "speed": 1.0})
            else:
                seg["target_timerange"]["duration"] = dur_us
                seg["source_timerange"]["duration"] = play_us
                seg["speed"] = round(play_us / dur_us, 4)
            continue
        video_segments.append({
            "id": _new_id(), "material_id": mid,
            "target_timerange": {"start": _us(clip.start_frame, fps), "duration": dur_us},
            "source_timerange": {"start": 0, "duration": dur_us},
            "extra_material_refs": comp_ids, "clip": _clip_block(),
            "common_keyframes": _scale_keyframes(clip, dims, canvas_aspect, dur_us) or [],
            "speed": 1.0})

    audio_mid = _new_id()
    audio_comp = []
    for kind, coll in _AUDIO_COMPANIONS:
        cid = _new_id()
        audio_comp.append(cid)
        collections[coll].append(_companion(kind, cid))

    sound_materials: List[dict] = []
    sound_tracks: List[list] = []                      # [end_us, segments]
    sounds = sorted((s for s in timeline.sounds if s.file_path and s.file_path.strip()),
                    key=lambda s: s.start_frame)
    for sound in sounds:
        start_us = _us(sound.start_frame, fps)
        if start_us >= total_us:
            continue
        length_us = min(_us(sound.duration_frames, fps) if sound.duration_frames > 0
                        else 1_000_000, total_us - start_us)
        mid = _new_id()
        comp = []
        for kind, coll in _AUDIO_COMPANIONS:
            cid = _new_id()
            comp.append(cid)
            collections[coll].append(_companion(kind, cid))
        sound_materials.append({
            "id": mid, "type": "extract_music",
            "duration": _us(sound.duration_frames, fps) if sound.duration_frames > 0 else length_us,
            "path": _forward(sound.file_path), "material_name": _basename(sound.file_path),
            "has_audio": True, "category_name": "local", "category_id": "",
            "source_platform": 0})
        index = next((i for i, t in enumerate(sound_tracks) if t[0] <= start_us), -1)
        if index < 0:
            sound_tracks.append([0, []])
            index = len(sound_tracks) - 1
        sound_tracks[index][1].append({
            "id": _new_id(), "material_id": mid,
            "target_timerange": {"start": start_us, "duration": length_us},
            "source_timerange": {"start": 0, "duration": length_us},
            "extra_material_refs": comp, "clip": _clip_block(),
            "common_keyframes": [], "speed": 1.0})
        sound_tracks[index][0] = start_us + length_us

    narration_material = {
        "id": audio_mid, "type": "extract_music", "duration": audio_us,
        "path": _forward(timeline.audio.file_path),
        "material_name": _basename(timeline.audio.file_path), "has_audio": True,
        "category_name": "local", "category_id": "", "source_platform": 0}
    tracks = [
        {"type": "video", "attribute": 0, "flag": 0, "segments": video_segments},
        {"type": "audio", "attribute": 0, "flag": 0, "segments": [{
            "id": _new_id(), "material_id": audio_mid,
            "target_timerange": {"start": 0, "duration": audio_us},
            "source_timerange": {"start": 0, "duration": audio_us},
            "extra_material_refs": audio_comp, "clip": _clip_block(),
            "common_keyframes": [], "speed": 1.0}]}]
    for _end, segments in sound_tracks:
        tracks.append({"type": "audio", "attribute": 0, "flag": 0, "segments": segments})

    draft = {
        "id": _new_id(), "version": "360000", "new_version": DRAFT_NEW_VERSION,
        "name": draft_name, "duration": max(total_us, audio_us), "create_time": 0,
        "update_time": 0, "fps": fps, "is_drop_frame_timecode": False, "color_space": 0,
        "config": json.loads(CONFIG_TEMPLATE),
        "canvas_config": {"ratio": f"{cw}:{ch}", "width": cw, "height": ch,
                          "background": None},
        "tracks": tracks, "group_container": [],
        "materials": {
            "videos": videos, "audios": [narration_material] + sound_materials,
            "canvases": collections["canvases"], "speeds": collections["speeds"],
            "sound_channel_mappings": collections["sound_channel_mappings"],
            "material_colors": collections["material_colors"],
            "placeholder_infos": collections["placeholder_infos"],
            "vocal_separations": collections["vocal_separations"], "transitions": []},
        "keyframes": json.loads(KEYFRAMES_TEMPLATE), "keyframe_graph_list": [],
        "platform": json.loads(PLATFORM_TEMPLATE),
        "last_modified_platform": json.loads(PLATFORM_TEMPLATE),
        "mutable_config": None, "cover": None, "retouch_cover": None, "extra_info": None,
        "relationships": [], "mixed_track_mode_on": False, "render_index_track_mode_on": True,
        "free_render_index_mode_on": False, "static_cover_image_path": "", "source": "default",
        "time_marks": [], "path": "", "lyrics_effects": [],
        "uneven_animation_template_info": json.loads(UNEVEN_TEMPLATE), "draft_type": "video",
        "smart_ads_info": json.loads(SMART_ADS_TEMPLATE),
        "function_assistant_info": json.loads(FUNCTION_ASSISTANT_TEMPLATE)}
    return dumps(draft)


def build_meta_info(timeline: m.Timeline, images: Sequence[m.ImageInfo], output_folder: str,
                    draft_name: str) -> str:
    now_s = int(time.time())
    now_us = int(time.time() * 1000) * 1000
    fps = timeline.fps
    clips = [c for s in timeline.scenes for c in s.clips]
    total_us = _us(sum(c.duration_frames for c in clips), fps)
    frames_by_file: Dict[str, int] = {}
    for c in clips:
        k = c.file_path.lower()
        frames_by_file[k] = frames_by_file.get(k, 0) + c.duration_frames

    entries = []
    for image in images:
        frames = frames_by_file.get(image.file_path.lower(), 0)
        entries.append({
            "ai_group_type": "", "create_time": now_s, "duration": _us(frames, fps),
            "enter_from": 0, "extra_info": _basename(image.file_path),
            "file_Path": _forward(image.file_path), "height": image.height,
            "id": _new_lower_id(), "import_time": now_s, "import_time_ms": now_us,
            "item_source": 1, "material_color_tag": "", "md5": "", "metetype": "photo",
            "roughcut_time_range": {"duration": -1, "start": -1},
            "sub_time_range": {"duration": -1, "start": -1}, "type": 0, "width": image.width})

    trimmed = output_folder.rstrip("\\/")
    root = re.split(r"[\\/]", trimmed)
    draft_root = "/".join(root[:-1]) if len(root) > 1 else output_folder
    meta = {
        "draft_id": _new_lower_id(), "draft_name": draft_name,
        "draft_fold_path": _forward(output_folder), "draft_root_path": _forward(draft_root),
        "draft_type": "video", "draft_new_version": DRAFT_NEW_VERSION, "draft_cover": "",
        "draft_is_invisible": False, "draft_removable_storage_device": "",
        "draft_need_rename_folder": False, "draft_timeline_materials_size_": 0,
        "draft_materials": [{"type": 0, "value": entries}],
        "draft_materials_copied_info": [], "draft_segment_extra_info": [],
        "tm_draft_create": now_us, "tm_draft_modified": now_us, "tm_draft_removed": 0,
        "tm_duration": total_us}
    return dumps(meta)


def export(timeline: m.Timeline, images: Sequence[m.ImageInfo], output_folder: str,
           freezes: Optional[Dict[str, str]] = None) -> str:
    """Write the draft folder; returns the draft_content.json path."""
    os.makedirs(output_folder, exist_ok=True)
    draft_name = os.path.basename(output_folder.rstrip("\\/"))
    content_path = os.path.join(output_folder, "draft_content.json")
    for name, text in (("draft_content.json", build_draft_content(timeline, images, draft_name, freezes)),
                       ("draft_meta_info.json",
                        build_meta_info(timeline, images, output_folder, draft_name))):
        with open(os.path.join(output_folder, name), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return content_path


# ---- templates (verbatim from the C# exporter) ---------------------------------------
PLATFORM_TEMPLATE = r'''{"os":"windows","os_version":"10.0.26200","app_id":359289,"app_version":"9.4.0","app_source":"cc","device_id":"6f45715d6efed8ecb2e77032cf767506","hard_disk_id":"","mac_address":"9f93f49408ae4068db25f5abb1ca86f9,0e5b23b3b9352421ccd763ace55e560f,ce0c79f29b088c447fa991bce98194a7"}'''

CONFIG_TEMPLATE = r'''{"video_mute":false,"record_audio_last_index":1,"extract_audio_last_index":1,"original_sound_last_index":1,"subtitle_recognition_id":"","subtitle_taskinfo":[],"lyrics_recognition_id":"","lyrics_taskinfo":[],"subtitle_sync":true,"lyrics_sync":true,"voice_change_sync":false,"sticker_max_index":1,"adjust_max_index":1,"material_save_mode":0,"export_range":null,"maintrack_adsorb":true,"combination_max_index":1,"attachment_info":[],"zoom_info_params":null,"system_font_list":[],"multi_language_mode":"none","multi_language_main":"none","multi_language_current":"none","multi_language_list":[],"subtitle_keywords_config":null,"use_float_render":false,"hdr_vivid":false}'''

KEYFRAMES_TEMPLATE = r'''{"videos":[],"audios":[],"texts":[],"stickers":[],"filters":[],"adjusts":[],"handwrites":[],"effects":[]}'''

UNEVEN_TEMPLATE = r'''{"composition":"","content":"","order":"","sub_template_info_list":[]}'''

SMART_ADS_TEMPLATE = r'''{"page_from":"","routine":"","draft_url":""}'''

FUNCTION_ASSISTANT_TEMPLATE = r'''{"smart_rec_applied":false,"fixed_rec_applied":false,"auto_adjust":false,"auto_adjust_segid_list":[],"color_correction":false,"color_correction_segid_list":[],"enhance_quality":false,"smooth_slow_motion":false,"deflicker_segid_list":[],"video_noise_segid_list":[],"enhance_quality_segid_list":[],"smart_segid_list":[],"retouch":false,"retouch_segid_list":[],"enhande_voice":false,"enhance_voice_segid_list":[],"audio_noise_segid_list":[],"auto_caption":false,"auto_caption_segid_list":[],"auto_caption_template_id":"","caption_opt":false,"caption_opt_segid_list":[],"eye_correction":false,"eye_correction_segid_list":[],"normalize_loudness":false,"normalize_loudness_audio_denoise_segid_list":[],"auto_adjust_fixed":false,"auto_adjust_fixed_value":50.0,"color_correction_fixed":false,"color_correction_fixed_value":50.0,"normalize_loudness_fixed":false,"enhande_voice_fixed":false,"retouch_fixed":false,"enhance_quality_fixed":false,"smooth_slow_motion_fixed":false,"fps":{"num":0,"den":1}}'''

VIDEO_MATERIAL_TEMPLATE = r'''{"id":"","unique_id":"","type":"photo","duration":10800000000,"path":"","media_path":"","local_id":"","has_audio":false,"reverse_path":"","intensifies_path":"","reverse_intensifies_path":"","intensifies_audio_path":"","cartoon_path":"","width":0,"height":0,"category_id":"","category_name":"local","material_id":"","material_name":"","material_url":"","crop":{"upper_left_x":0.0,"upper_left_y":0.0,"upper_right_x":1.0,"upper_right_y":0.0,"lower_left_x":0.0,"lower_left_y":1.0,"lower_right_x":1.0,"lower_right_y":1.0},"crop_ratio":"free","audio_fade":null,"crop_scale":1.0,"extra_type_option":0,"stable":{"stable_level":0,"matrix_path":"","time_range":{"start":0,"duration":0}},"matting":{"flag":0,"path":"","interactiveTime":[],"has_use_quick_brush":false,"strokes":[],"has_use_quick_eraser":false,"expansion":0,"feather":0,"reverse":false,"custom_matting_id":"","enable_matting_stroke":false,"is_clould":false,"mask_video_path":"","cloud_product_fps":0.0},"source":0,"source_platform":0,"formula_id":"","check_flag":62978047,"video_algorithm":{"algorithms":[],"time_range":null,"path":"","gameplay_configs":[],"ai_in_painting_config":[],"complement_frame_config":null,"motion_blur_config":null,"deflicker":null,"noise_reduction":null,"quality_enhance":null,"super_resolution":null,"ai_background_configs":[],"smart_complement_frame":null,"aigc_generate":null,"aigc_generate_list":[],"mouth_shape_driver":null,"ai_expression_driven":null,"ai_motion_driven":null,"image_interpretation":null,"story_video_modify_video_config":null,"skip_algorithm_index":[]},"is_unified_beauty_mode":false,"is_set_beauty_mode":false,"object_locked":null,"smart_motion":null,"multi_camera_info":null,"freeze":null,"picture_from":"none","picture_set_category_id":"","picture_set_category_name":"","team_id":"","local_material_id":"","origin_material_id":"","request_id":"","has_sound_separated":false,"is_text_edit_overdub":false,"is_ai_generate_content":false,"is_video_copilot_aigc_content":false,"aigc_type":"none","is_copyright":false,"aigc_history_id":"","aigc_item_id":"","local_material_from":"","smart_match_info":null,"beauty_face_preset_infos":[],"beauty_body_preset_id":"","beauty_face_auto_preset":{"preset_id":"","name":"","rate_map":"","scene":""},"beauty_face_auto_preset_infos":[],"beauty_body_auto_preset":null,"live_photo_timestamp":-1,"live_photo_cover_path":"","content_feature_info":null,"corner_pin":null,"surface_trackings":[],"video_mask_stroke":{"resource_id":"","path":"","type":"","color":"","size":0.0,"alpha":0.0,"distance":0.0,"texture":0.0,"horizontal_shift":0.0,"vertical_shift":0.0},"video_mask_shadow":{"resource_id":"","path":"","color":"","alpha":0.0,"blur":0.0,"distance":0.0,"angle":0.0},"pre_applied_vip_materials":[],"workflow_node_id":""}'''
