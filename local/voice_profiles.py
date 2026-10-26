"""Server-owned, versioned synthesis inputs; project JSON selects an ID, not code or paths."""
from copy import deepcopy
import os
from pathlib import Path
import sys

MODEL = os.environ.get('TALETONE_TTS_MODEL', 'mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit')
MODEL_REVISION = os.environ.get('TALETONE_TTS_MODEL_REVISION', '1c6c0ff58c43afa8df571facde2efa077efd85e2')
ALGORITHM_VERSION = 'published-dialogue-pcm24k-atempo1.2-loudnorm-v1'
MLX_PYTHON = str(Path(os.environ.get('TALETONE_MLX_PYTHON', sys.executable)).expanduser())
RATE = 24000

_COMMON = {'model': MODEL, 'model_revision': MODEL_REVISION,
           'mlx_audio_version': os.environ.get('TALETONE_MLX_AUDIO_VERSION', '0.5.7'),
           'mlx_version': os.environ.get('TALETONE_MLX_VERSION', '0.32.3'),
           'rate': RATE, 'temperature': 0.65, 'max_tokens': 1024,
           'seed': 17, 'max_tempo': 1.2, 'slot_margin_ms': 60,
           'normalization': 'loudnorm=I=-16:TP=-1.5:LRA=7',
           'codec': 'aac', 'bitrate': '160k', 'algorithm': ALGORITHM_VERSION}
_PROFILES = {
    'ja-playful-v1': {**_COMMON, 'voice': 'Ono_Anna', 'lang_code': 'Japanese', 'language': 'ja',
        'instruction': ('日本語のオリジナルのアニメ少年ナレーター。いたずらっぽく、少し鼻にかかったコミカルな声。'
                        'わくわくした興奮、豊かな抑揚、言葉の強弱と自然な間。元気だが叫ばない。'
                        '聞き取りやすいやや軽快な速さで、重要な数字ははっきり発音。'
                        '落ち着いた朗読や機械的な早口にはしない。')},
    'zh-beijing-v1': {**_COMMON, 'voice': 'Dylan', 'lang_code': 'Chinese', 'language': 'zh',
        'instruction': '用标准普通话讲述，三十岁左右的男性，声音温暖柔和、自然亲切，像平静地向朋友分享经历。中低音，语调舒缓，情绪克制，不要播音腔，不要夸张，不要耳语。'},
    'zh-chengdu-v1': {**_COMMON, 'voice': 'Eric', 'lang_code': 'Chinese', 'language': 'zh',
        'instruction': '用地道的四川成都话和成都口音，通用成年男性音色，做抓人的四川评书式讲述。像向满堂熟人摆一桩稀奇事：铺垫时放低、放慢、吊胃口，关键处先顿一下，讲到金额、意外和转折时突然拔高、惊叹，反问有劲，吐槽带自嘲，包袱抖开以后再收回来。语调大起大落，一惊一乍，有张有弛，让人想接着听；不是全程喊叫，也不是温柔平淡地念稿。保持自然成都话，不要普通话播音腔。不模仿任何具体真人的声线。只表演文本已有的内容，不增加字句、事实、笑声、音效或歌唱；英文品牌、数字、专业术语清楚准确。'}
}


def profile(profile_id):
    """Return a private snapshot, so no caller can mutate subsequent jobs' inputs."""
    if not isinstance(profile_id, str) or profile_id not in _PROFILES:
        raise ValueError('Unknown generated voice profile')
    return deepcopy(_PROFILES[profile_id])
