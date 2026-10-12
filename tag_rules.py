# -*- coding: utf-8 -*-
"""
标签分类规则表。
规则按顺序匹配，命中即停。导入、反推时 danbooru 表没意见的词由它兜底归类，可以直接改这个文件。
只认旧六段（画风 / 人物 / 服饰 / 姿势 / 镜头 / 环境），见 importer.RULE_COVERS。

mode 说明:
  exact    = 标签完全等于该词
  suffix   = 标签以该词结尾
  prefix   = 标签以该词开头
  contains = 标签里包含该词
"""

BUCKETS = ["artist", "character", "outfit", "pose", "camera", "scene"]

BUCKET_CN = {
    "artist": "画风",
    "character": "人物",
    "outfit": "服饰",
    "pose": "姿势",
    "camera": "镜头",
    "scene": "环境",
    "unknown": "未归类",
}

CN_BUCKET = {v: k for k, v in BUCKET_CN.items()}

# ===================================================================
# 规则表：(目标桶, 匹配方式, [词...])
# 顺序非常重要，从上往下匹配，先命中先归类。
# ===================================================================
RULES = [

    # --- ① 必须最先拦截的人物词（否则会被服饰/镜头规则抢走）---
    ("character", "exact", [
        "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls",
        "1boy", "2boys", "3boys", "multiple boys", "1other",
        "solo", "male focus", "female focus", "girl", "boy", "woman", "man",
        "mature female", "mature male", "young", "child", "loli", "shota",
        "older", "aged up", "aged down", "androgynous", "otoko no ko",
    ]),
    ("character", "suffix", [
        " hair", " eyes", " eye", " skin", " breasts", " ears", " tail",
        " horns", " wings", " pupils", " eyelashes", " eyebrows", " sclera",
    ]),
    ("character", "exact", [
        "hair", "eyes", "breasts", "ears", "tail", "wings", "horns",
        "collarbone", "sunburn", "suntan", "wet", "colored tips",
        "colored inner hair", "streaked hair", "two-tone hair",
    ]),

    # --- ② 镜头 / 构图 / 视角 ---
    ("camera", "exact", [
        "from above", "from below", "from side", "from behind", "from front",
        "close-up", "extreme close-up", "portrait", "upper body", "lower body",
        "cowboy shot", "full body", "wide shot", "medium shot", "bust",
        "dutch angle", "straight-on", "profile", "three-quarter view",
        "fisheye", "panorama", "perspective", "foreshortening", "vanishing point",
        "solo focus", "face focus", "eye focus", "foot focus", "hand focus",
        "breast focus", "ass focus", "back focus",
        "depth of field", "blurry", "blurry background", "blurry foreground",
        "bokeh", "motion blur", "chromatic aberration", "lens flare",
        "pov", "pov hands", "first-person view", "viewer holding",
        "cropped", "cropped legs", "cropped torso", "cropped arms",
        "head out of frame", "feet out of frame", "hands out of frame",
        "out of frame", "cut in", "split screen", "multiple views",
        "zoom layer", "rotated", "sideways", "upside-down", "tilted",
        "wide angle", "telephoto", "macro", "symmetry", "rule of thirds",
        "dynamic angle", "dramatic angle", "low angle", "high angle",
        "eye-level shot", "overhead shot", "worm's-eye view", "bird's-eye view",
    ]),
    ("camera", "contains", [
        "focus on", " shot", "angle view",
    ]),

    # --- ③ 服饰 / 配饰 ---
    ("outfit", "exact", [
        "ring", "pin", "tie", "hat", "cap", "bow", "clip", "band",
    ]),
    ("outfit", "suffix", [
        " ring", " pin", " tie", " hat", " cap", " clip", " band",
    ]),
    ("outfit", "prefix", [
        "hat ", "cap ", "ring ",
    ]),
    ("outfit", "contains", [
        "shirt", "blouse", "skirt", "dress", "pants", "trousers", "shorts",
        "jeans", "jacket", "coat", "hoodie", "sweater", "cardigan", "vest",
        "blazer", "uniform", "suit", "kimono", "yukata", "hakama", "haori",
        "qipao", "cheongsam", "hanfu", "sari", "swimsuit", "bikini", "one-piece",
        "leotard", "bodysuit", "catsuit", "apron", "cape", "cloak", "robe",
        "cassock", "habit", "armor", "pauldron", "gauntlet", "breastplate",
        "underwear", "panties", "bra", "lingerie", "garter", "corset",
        "camisole", "nightgown", "pajamas", "negligee", "thong", "briefs",
        "socks", "thighhighs", "kneehighs", "pantyhose", "stockings", "legwear",
        "shoes", "boots", "sandals", "heels", "sneakers", "loafers", "slippers",
        "geta", "zori", "footwear",
        "gloves", "mittens", "armband", "armlet", "wristband", "bracer",
        "beret", "hood", "helmet", "headgear", "headwear",
        "headband", "hairband", "hairclip", "hair ornament", "hair ribbon",
        "hair bow", "hair flower", "scrunchie", "headdress", "veil", "crown",
        "tiara", "circlet", "halo",
        "necklace", "choker", "collar", "pendant", "earring", "piercing",
        "bracelet", "anklet", "jewelry", "brooch", "badge",
        "belt", "buckle", "sash", "obi", "suspenders", "harness", "strap",
        "necktie", "bowtie", "scarf", "shawl", "muffler", "bandana",
        "glasses", "sunglasses", "eyewear", "goggles", "monocle",
        "headphones", "earphones", "headset", "tassel", "fringe", "pompom",
        "mask", "visor", "eyepatch", "blindfold", "bandage",
        "sleeve", "cuff", "frill", "lace", "ruffle", "button", "zipper",
        "pocket", "hem", "collarbone cutout", "cutout", "cleavage cutout",
        "clothes", "clothing", "costume", "outfit", "attire", "garment",
        "pantsuit", "overalls", "romper", "jumpsuit", "poncho", "towel",
        "striped", "plaid", "polka dot", "checkered", "argyle", "camouflage",
        "print", "logo", "emblem", "trim", "embroidery",
    ]),
    ("outfit", "exact", [
        "nude", "naked", "topless", "bottomless", "undressing", "partially undressed",
        "clothes lift", "skirt lift", "shirt lift", "clothes pull", "wardrobe malfunction",
        "see-through", "transparent", "wet clothes", "torn clothes", "casual",
        "formal", "school uniform", "serafuku", "maid", "nun", "witch", "idol",
    ]),

    # --- ③.5 容易被动作词抢走的道具/景物短语，必须先拦 ---
    ("scene", "exact", [
        "drinking glass", "drinking straw", "drinking cup", "drinking fountain",
        "palm tree", "christmas tree", "cherry blossoms", "falling leaves",
        "shooting star", "rocking chair", "beach chair", "lounge chair",
    ]),

    # --- ④ 姿势 / 动作 / 表情 / 视线 ---
    ("pose", "contains", [
        "standing", "sitting", "lying", "kneeling", "squatting", "crouching",
        "leaning", "bending", "bent over", "walking", "running", "jumping",
        "flying", "floating", "falling", "swimming", "dancing", "stretching",
        "holding", "grabbing", "gripping", "carrying", "lifting", "hugging",
        "embracing", "touching", "pointing", "reaching", "waving", "pulling",
        "pushing", "throwing", "catching", "covering", "hiding", "clenching",
        "drinking", "eating", "biting", "licking", "kissing", "smoking",
        "sleeping", "resting", "sighing", "yawning", "breathing",
        "looking", "staring", "glancing", "gazing", "watching", "peeking",
        "smiling", "smile", "grin", "laughing", "giggling", "blushing", "blush",
        "crying", "tears", "frown", "pout", "sulking", "scowl", "glare",
        "surprised", "shocked", "embarrassed", "nervous", "angry", "sad",
        "happy", "excited", "confused", "worried", "scared", "sleepy",
        "open mouth", "closed mouth", "closed eyes", "one eye closed", "wink",
        "half-closed eyes", "wide-eyed", "rolling eyes", "heart-shaped pupils",
        "tongue", "teeth", "fang", "drooling", "saliva", "sweat", "sweatdrop",
        "hand on", "hands on", "hand up", "hands up", "hand in", "hands in",
        "arm up", "arms up", "arm behind", "arms behind", "arm across",
        "crossed arms", "crossed legs", "spread legs", "legs up", "legs apart",
        "knees up", "knee up", "on back", "on side", "on stomach", "all fours",
        "fetal position", "indian style", "wariza", "seiza", "yokozuwari",
        "v sign", "peace sign", "thumbs up", "salute", "shrug", "facepalm",
        "head tilt", "tilting head", "head back", "head down", "leaning forward",
        "outstretched", "akimbo", "contrapposto", "dynamic pose", "action pose",
        "straddling", "riding", "piggyback", "princess carry", "carrying over shoulder",
        "finger", "fist", "clapping", "crossed fingers",
        "tiptoes", "on tiptoes",
    ]),
    ("pose", "exact", [
        ":d", ":o", ":3", ":p", ":>", ":<", "^_^", "xd", ";)", ";d",
        "expressionless", "serious", "smug", "seductive smile", "parted lips",
        "eye contact", "looking at viewer", "looking away", "looking back",
        "looking down", "looking up", "looking to the side", "eyes visible through hair",
        "invisible chair", "posing", "pose",
    ]),

    # --- ⑤ 环境 / 背景 / 光照 / 天气 / 时间 ---
    ("scene", "exact", [
        "bar", "pub", "club", "inn", "pier", "dock", "port",
        "english text", "text", "speech bubble", "border", "letterboxed",
        "watermark", "signature", "artist name", "logo", "frame",
    ]),
    ("scene", "contains", [
        "background", "outdoors", "indoors", "scenery", "landscape", "cityscape",
        "sky", "cloud", "sun", "sunlight", "sunbeam", "moon", "moonlight",
        "star", "starry", "aurora", "rainbow", "horizon",
        "day", "night", "dusk", "dawn", "twilight", "evening", "morning",
        "noon", "midnight", "sunset", "sunrise", "golden hour",
        "rain", "snow", "storm", "thunder", "lightning", "fog", "mist", "haze",
        "wind", "weather", "overcast", "cloudy", "sunny",
        "spring", "summer", "autumn", "fall", "winter",
        "water", "ocean", "sea", "beach", "shore", "sand", "wave", "river",
        "lake", "pond", "puddle", "waterfall", "pool", "bath", "onsen",
        "underwater", "reflection", "ripple",
        "tree", "forest", "woods", "jungle", "grass", "field", "meadow",
        "flower", "petal", "leaf", "leaves", "bush", "garden", "park",
        "mountain", "hill", "cliff", "valley", "cave", "desert", "island",
        "volcano", "glacier", "ruins", "rock", "stone",
        "city", "town", "village", "street", "road", "alley", "sidewalk",
        "building", "house", "tower", "bridge", "fence", "wall", "gate",
        "stairs", "rooftop", "balcony", "window", "door", "curtain",
        "room", "bedroom", "bathroom", "kitchen", "classroom", "office",
        "library", "cafe", "restaurant", "shop", "store", "station",
        "train", "car", "bus", "boat", "ship", "airplane",
        "shrine", "temple", "church", "castle", "torii", "dojo",
        "bed", "chair", "sofa", "couch", "table", "desk", "shelf", "floor",
        "ceiling", "lamp", "candle", "lantern", "fireplace",
        "light", "lighting", "shadow", "silhouette", "backlighting", "rim light",
        "god rays", "light rays", "glow", "sparkle", "glitter", "bloom",
        "fire", "flame", "smoke", "steam", "dust", "bubble", "confetti",
        "firework", "explosion", "snowflake", "falling petals",
    ]),

    # --- ⑥ 剩下的身体部位 / 体征 → 人物 ---
    ("character", "contains", [
        "navel", "stomach", "midriff", "cleavage", "collarbone", "shoulder",
        "armpit", "back", "waist", "hip", "thigh", "leg", "knee", "ankle",
        "foot", "feet", "toe", "arm", "elbow", "hand", "nail", "lips",
        "nose", "cheek", "chin", "forehead", "neck", "throat", "tongue",
        "muscle", "muscular", "abs", "slim", "petite", "curvy", "plump",
        "thick", "tall", "short", "height", "body", "figure",
        "mole", "freckle", "scar", "tattoo", "birthmark", "bandaid",
        "dark skin", "pale skin", "tan", "sunburn",
        "nipple", "areola", "pussy", "penis", "anus", "genital",
        "bangs", "ponytail", "twintail", "braid", "bun", "ahoge", "sidelock",
        "antenna hair", "drill", "curl", "updo", "undercut", "hime cut",
        "animal ear", "cat ear", "fox ear", "dog ear", "rabbit ear",
        "elf", "demon", "angel", "vampire", "robot", "android", "mecha",
        "bare arms", "bare legs", "bare shoulders", "barefoot",
    ]),

    # --- ⑦ 道具 → 环境（可按需改成 pose）---
    ("scene", "contains", [
        "cup", "mug", "glass", "bottle", "can ", "straw", "plate", "bowl",
        "food", "fruit", "cake", "candy", "ice cream", "drink", "beverage",
        "tea", "coffee", "juice", "alcohol", "wine", "beer", "cocktail",
        "book", "notebook", "paper", "pen", "pencil", "phone", "camera",
        "bag", "basket", "box", "umbrella", "fan", "mirror", "clock",
        "sword", "knife", "gun", "rifle", "weapon", "shield", "staff", "wand",
        "bow and arrow", "spear", "axe", "hammer",
        "instrument", "guitar", "piano", "violin", "microphone",
        "toy", "doll", "plush", "balloon", "ball", "flower bouquet",
        "animal", "cat", "dog", "bird", "fish", "butterfly", "horse",
    ]),
]


def normalize(tag: str) -> str:
    t = tag.strip().lower().replace("_", " ")
    while "  " in t:
        t = t.replace("  ", " ")
    return t


def parse_override(text: str):
    """
    解析用户额外规则。每行格式：
        桶名: 词1, 词2, 词3
    桶名可用中文（画风/人物/服饰/姿势/镜头/环境）或英文。
    这里的词按「完全匹配」优先处理，优先级高于内置规则。
    """
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line and "：" not in line:
            continue
        line = line.replace("：", ":", 1)
        name, words = line.split(":", 1)
        name = name.strip()
        bucket = CN_BUCKET.get(name, name.strip().lower())
        if bucket not in BUCKETS:
            continue
        ws = [normalize(w) for w in words.split(",") if w.strip()]
        if ws:
            out.append((bucket, ws))
    return out


def classify(tag: str, overrides) -> str:
    t = normalize(tag)
    if not t:
        return None

    for bucket, words in overrides:
        for w in words:
            if t == w or w in t:
                return bucket

    for bucket, mode, words in RULES:
        for w in words:
            if mode == "exact" and t == w:
                return bucket
            if mode == "suffix" and t.endswith(w):
                return bucket
            if mode == "prefix" and t.startswith(w):
                return bucket
            if mode == "contains" and w in t:
                return bucket
    return "unknown"
