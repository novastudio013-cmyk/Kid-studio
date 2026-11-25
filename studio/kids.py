"""kids.py - Pip & Friends: 3D preschool video pipeline (one file).

Every picture is a real Blender render of a characters built from primitives, so the
character is identical in every scene and every episode. No image API, no rate limits,
no 2-per-scene lottery.

  python kids.py --format short --upload            # scheduled production run
  python kids.py --format short --dry-run           # build, check, do not upload
  python kids.py --format short --plan-only         # just print the story + shot plan
  python kids.py --test --no-blender                # end-to-end plumbing test, proxy art

Stages: script -> shot plan -> Blender stills -> 2.5D motion + voice -> music -> QA -> upload
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STUDIO = os.path.join(HERE, "studio3d.py")
HISTORY = os.path.join(HERE, "history.json")

DEFAULTS = {
    "channel_name": "Starlit Stories",
    "character_name": "Pip",
    "voice": "en-US-JennyNeural",
    "voice_rate": "-8%",
    "voice_pitch": "+4Hz",
    "kinds": ["counting", "colors", "guess_animal", "story"],
    "short_scenes": 6,
    "long_scenes": 12,
    "quality": "high",      # draft | high | ultra  (see QUALITY below)
    "samples": None,        # None = take it from the quality preset
    "lipsync": True,        # animate the mouth from Edge TTS word timings
    "mouth_fps": None,      # None = take it from the quality preset
    "fps": 30,
    "blender": "",          # empty = find on PATH
    "music": True,
    "outro_line": "See you next time!",
    "outro_line_local": {"es": "¡Hasta la próxima!", "pt": "Até a próxima!"},
    "outro_card_local": {"es": "¡Hasta pronto!", "pt": "Até logo!"},
    "languages": ["en"],       # one render, one video per language
    "made_for_kids": True,
    "privacy": "private",
    "language": "en",
}

SHORT = (1080, 1920)
LONG = (1920, 1080)

# Quality presets. "render" is what Blender draws; the encoder downsamples to the
# delivery size, so supersample > 1 buys crisp edges cheaply.
QUALITY = {
    "draft": dict(supersample=1.0, samples=16,  crf=20, preset="veryfast",
                  denoise=False, mouth_fps=12),
    "high":  dict(supersample=1.5, samples=32,  crf=16, preset="medium",
                  denoise=True,  mouth_fps=30),
    "ultra": dict(supersample=2.0, samples=64,  crf=14, preset="slow",
                  denoise=True,  mouth_fps=30),
}


def free_ram_gb():
    """Free RAM in GB. Reads MemAvailable, which is what actually matters in a container."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except Exception:
        return 4.0


def resolve_quality(cfg):
    """Merge the preset with anything set explicitly in config.json."""
    name = str(cfg.get("quality", "high")).lower()
    q = dict(QUALITY.get(name, QUALITY["high"]))
    for k in ("samples", "crf", "preset", "denoise", "mouth_fps", "supersample"):
        if cfg.get(k) is not None:
            q[k] = cfg[k]
    q["name"] = name
    # OIDN needs real RAM; on a small box it gets OOM-killed and takes the run with it
    if q["denoise"]:
        avail = free_ram_gb()
        if avail < 3.0:
            print("  denoise off: only %.1f GB RAM free" % avail)
            q["denoise"] = False
            q["samples"] = max(q["samples"], 48)   # more samples instead of denoise
    return q


# --------------------------------------------------------------------------- utils
def load_cfg():
    cfg = dict(DEFAULTS)
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            cfg.update(json.load(open(p)))
        except Exception as e:
            print("config.json problem, using defaults:", e)
    return cfg


def run(cmd, quiet=True, check=True):
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL if quiet else None,
                       stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise RuntimeError("command failed: %s\n%s" % (cmd[0], r.stderr.decode(errors="ignore")[-800:]))
    return r


def probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def find_blender(cfg):
    for c in (cfg.get("blender"), os.environ.get("BLENDER"), shutil.which("blender"),
              "/usr/local/blender/blender", "/opt/blender/blender"):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return None


# --------------------------------------------------------------------------- script (local, no API)
# The grammar emits *structured* lines: a template key plus parameters, never a finished
# English string. That is what makes one render reusable in every language - localize()
# re-renders the same keys through another language pack.

LANG = {
    "en": dict(
        voice="en-US-JennyNeural", label="English", lang_tag="en",
        words={1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six",
               7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten", 11: "Eleven", 12: "Twelve"},
        things={"balloon": ("balloon", "balloons", "m"), "apple": ("apple", "apples", "m"),
                "star": ("star", "stars", "f"), "ball": ("ball", "balls", "f")},
        count_intro=["Let us count {thing}s together!", "Time to count! Are you ready?",
                     "Look at all these {thing}s!"],
        count_rhyme=["{n} little {thing}{s}, floating up so high!",
                     "{n} little {thing}{s}, waving at the sky!",
                     "{n} little {thing}{s}, hooray and hooray!",
                     "{n} little {thing}{s}, come and play!",
                     "Here is one {thing}. Now there are {n}!",
                     "{n} happy {thing}{s}, all in a line!",
                     "One, two, {n}! Count them with me!",
                     "{n} {thing}{s} bouncing, happy as can be!"],
        count_outro=["We counted {n} {thing}{s}. Well done!",
                     "You counted every one. Hooray for you!",
                     "{n} {thing}{s}, all counted. Clever you!"],
        colors={"red": "red", "blue": "blue", "yellow": "yellow", "green": "green",
                "orange": "orange", "purple": "purple", "pink": "pink", "brown": "brown"},
        color_line=["I see {c}, {c}, {c}! {C}, {c}, and one more {c}!",
                    "Look, it is {c}! {C} like a happy {c} thing!",
                    "{C}! {C}! Can you find something {c}?",
                    "Here is {c}. Do you like {c}? I do!"],
        clues=["I am small and round. I love to play. Who am I?",
               "Listen to my sound. Can you guess? Who am I?",
               "I have a secret. Only you can guess. Who am I?"],
        animal_names={"duck": "Duck", "frog": "Frog", "cat": "Cat"},
        animals={"duck": ("Quack quack", "I have a little bill and two orange feet."),
                 "frog": ("Ribbit ribbit", "I hop and jump and love the pond."),
                 "cat": ("Meow meow", "I have soft whiskers and a swishy tail.")},
        lesson_names={"sharing": "Sharing", "trying again": "Trying Again",
                      "being kind": "Being Kind", "saying sorry": "Saying Sorry",
                      "being brave": "Being Brave", "sharing a snack": "Sharing a Snack"},
        stories={
            "sharing": ["{c} finds one big red ball.", "A friend wants to play too.",
                        "{c} rolls the ball to the friend.", "Two friends, one ball, so much fun!"],
            "trying again": ["{c} wants to reach a tall balloon.", "{c} hops once. Not yet!",
                             "{c} hops twice. So close!", "One more hop. Hooray, {c} can do it!"],
            "being kind": ["{c} sees a friend feeling small.", "{c} gives a warm smile.",
                           "{c} waves a gentle wing.", "The friend smiles back. Kindness is lovely!"],
            "saying sorry": ["{c} bumps into a friend. Oh no!", "{c} says sorry, softly.",
                             "The friend smiles. It is okay!", "Saying sorry makes friends happy!"],
            "being brave": ["The sky goes a little bit dark.", "{c} feels shy and small.",
                            "{c} takes one slow, brave breath.", "{c} is brave. Everything is fine!"],
            "sharing a snack": ["{c} has two juicy apples.", "A friend has none at all.",
                                "{c} gives one apple away.", "Sharing makes the apples taste sweeter!"],
        },
        beats=["{c} wakes up. The sun is warm. What a lovely day!",
               "{c} sees something wonderful. Oh, how nice!",
               "{c} feels happy. We can do it together!",
               "Goodnight, little friends. Sleep tight!",
               "{c} takes a big breath and tries again.",
               "Look! {c} found a friend to help."],
        thumbs={"counting": "COUNT WITH PIP", "colors": "COLOR HUNT",
                "guess_animal": "WHO AM I?", "story": "BE KIND"},
        titles={
            "counting": ["{c} Counts {n} {things}", "Count to {n} with {c}",
                         "{n} Little {things} with {c}", "{c}'s Counting Day",
                         "Let Us Count, {c}!", "{c} and the {n} {things}"],
            "colors": ["{c}'s Color Hunt: {list}", "Learn Colours with {c}: {list}",
                       "{c} Finds {list}", "Colour Hunt! {list}"],
            "guess_animal": ["Who Am I? {animals} Guessing Game", "Guess the Animal: {animals}!",
                             "Can You Guess? {animals}", "Animal Sounds: {animals}"],
            "story": ["{c} and the Little {lesson}", "{c} Learns About {lesson}",
                      "A Story About {lesson}", "{c}'s {lesson} Day"],
        },
        blurb={"counting": "{c} counts {things}, one by one. A calm, cheerful counting video for toddlers.",
               "colors": "{c} hunts for colours and names each one along the way.",
               "guess_animal": "{c} plays a guessing game. Listen to the clue, then meet the animal.",
               "story": "A gentle story about {lesson} for little ones, with {c}."},
        tags={"counting": ["counting", "numbers for toddlers", "learn to count"],
              "colors": ["colors for toddlers", "learn colors", "colour video for kids"],
              "guess_animal": ["guess the animal", "animal sounds for kids", "toddler guessing game"],
              "story": ["bedtime story", "kids stories", "kindness for kids"]},
    ),

    "es": dict(
        voice="es-MX-DaliaNeural", label="Spanish", lang_tag="es",
        words={1: "Uno", 2: "Dos", 3: "Tres", 4: "Cuatro", 5: "Cinco", 6: "Seis",
               7: "Siete", 8: "Ocho", 9: "Nueve", 10: "Diez", 11: "Once", 12: "Doce"},
        things={"balloon": ("globo", "globos", "m"), "apple": ("manzana", "manzanas", "f"),
                "star": ("estrella", "estrellas", "f"), "ball": ("pelota", "pelotas", "f")},
        words_fem={1: "Una"},
        count_intro=["¡Vamos a contar {thing}s juntos!", "¡Hora de contar! ¿Estás listo?",
                     "¡Mira todos estos {thing}s!"],
        count_rhyme=["¡{n} {thing}{s}! ¡Uno, dos, tres!",
                     "¡{n} {thing}{s} para contar!",
                     "¡{n} {thing}{s}! ¡Mira nada más!",
                     "Aquí hay uno. ¡Ahora hay {n}!",
                     "¡{n} {thing}{s}, uno por uno!",
                     "¡Cuenta conmigo! ¡{n} {thing}{s}!",
                     "¡{n} {thing}{s}! ¡Qué bonito es contar!"],
        count_outro=["Contamos {n} {thing}{s}. ¡Muy bien!",
                     "¡Ya contamos! ¡Bravo por ti!",
                     "¡{n} {thing}{s}! ¡Ya está!"],
        colors={"red": "rojo", "blue": "azul", "yellow": "amarillo", "green": "verde",
                "orange": "naranja", "purple": "morado", "pink": "rosa", "brown": "marrón"},
        color_line=["¡Veo {c}, {c}, {c}! ¡{C}, {c} y más {c}!",
                    "¡Mira, es {c}! ¡{C} como un juguete {c}!",
                    "¡{C}! ¡{C}! ¿Puedes encontrar algo {c}?",
                    "Aquí está {c}. ¿Te gusta {c}? ¡A mí sí!"],
        clues=["Soy pequeño y redondo. Me encanta jugar. ¿Quién soy?",
               "Escucha mi sonido. ¿Puedes adivinar? ¿Quién soy?",
               "Tengo un secreto. Solo tú puedes adivinar. ¿Quién soy?"],
        animal_names={"duck": "Pato", "frog": "Rana", "cat": "Gato"},
        animals={"duck": ("¡Cuac cuac!", "Tengo un pico pequeño y dos patas naranjas."),
                 "frog": ("¡Croac croac!", "Salto y brinco y me encanta el estanque."),
                 "cat": ("¡Miau miau!", "Tengo bigotes suaves y una cola que se mueve.")},
        lesson_names={"sharing": "Compartir", "trying again": "Intentarlo Otra Vez",
                      "being kind": "Ser Amable", "saying sorry": "Pedir Perdón",
                      "being brave": "Ser Valiente", "sharing a snack": "Compartir la Merienda"},
        stories={
            "sharing": ["{c} encuentra una pelota roja grande.", "Un amigo también quiere jugar.",
                        "{c} rueda la pelota al amigo.", "¡Dos amigos, una pelota, mucha diversión!"],
            "trying again": ["{c} quiere alcanzar un globo alto.", "{c} salta una vez. ¡Todavía no!",
                             "{c} salta dos veces. ¡Casi!", "¡Un salto más. {c} lo logró!"],
            "being kind": ["{c} ve un pajarito que se siente pequeño.", "{c} le da una sonrisa cálida.",
                           "{c} mueve un ala con suavidad.", "¡El pajarito sonríe. La bondad es bella!"],
            "saying sorry": ["{c} choca con la torre de un amigo. ¡Ay!", "{c} pide perdón, bajito.",
                             "Construyen otra vez juntos.", "¡Dos ayudantes son mejores que uno!"],
            "being brave": ["El cielo se pone un poco oscuro.", "{c} se siente pequeño y tímido.",
                            "{c} respira despacio y con valentía.", "¡{c} es valiente. Todo está bien!"],
            "sharing a snack": ["{c} tiene dos manzanas jugosas.", "Un amigo no tiene ninguna.",
                                "{c} regala una manzana.", "¡Compartir hace la manzana más dulce!"],
        },
        beats=["{c} se despierta. El sol calienta. ¡Qué día tan bonito!",
               "{c} ve algo maravilloso. ¡Qué bien!",
               "{c} se siente feliz. ¡Podemos juntos!",
               "Buenas noches, amiguitos. ¡Dulces sueños!",
               "{c} respira hondo y lo intenta otra vez.",
               "¡Mira! {c} encontró un amigo que ayuda."],
        thumbs={"counting": "CUENTA CON PIP", "colors": "CAZA DE COLORES",
                "guess_animal": "¿QUIÉN SOY?", "story": "SÉ AMABLE"},
        titles={
            "counting": ["{c} cuenta {n} {things}", "Cuenta hasta {n} con {c}",
                         "{n} {things} con {c}", "El día de contar de {c}"],
            "colors": ["{c} y los colores: {list}", "Aprende colores con {c}: {list}",
                       "{c} encuentra {list}"],
            "guess_animal": ["¿Quién soy? {animals}", "Adivina el animal: {animals}",
                             "¿Puedes adivinar? {animals}"],
            "story": ["{c} y {lesson}", "{c} aprende a {lesson}", "Un cuento sobre {lesson}"],
        },
        blurb={"counting": "{c} cuenta {things}, uno por uno. Un video tranquilo y alegre para pequeños.",
               "colors": "{c} busca colores y nombra cada uno.",
               "guess_animal": "{c} juega a adivinar. Escucha la pista y conoce al animal.",
               "story": "Un cuento tierno sobre {lesson} para los más pequeños, con {c}."},
        tags={"counting": ["contar para niños", "números para bebés", "aprender a contar"],
              "colors": ["colores para niños", "aprender colores", "video de colores"],
              "guess_animal": ["adivina el animal", "sonidos de animales", "juego para niños"],
              "story": ["cuento para dormir", "cuentos infantiles", "historias para niños"]},
    ),

    "pt": dict(
        voice="pt-BR-FranciscaNeural", label="Portuguese", lang_tag="pt-BR",
        words={1: "Um", 2: "Dois", 3: "Três", 4: "Quatro", 5: "Cinco", 6: "Seis",
               7: "Sete", 8: "Oito", 9: "Nove", 10: "Dez", 11: "Onze", 12: "Doze"},
        things={"balloon": ("balão", "balões", "m"), "apple": ("maçã", "maçãs", "f"),
                "star": ("estrela", "estrelas", "f"), "ball": ("bola", "bolas", "f")},
        words_fem={1: "Uma", 2: "Duas"},
        count_intro=["Vamos contar {thing}s juntos!", "Hora de contar! Você está pronto?",
                     "Olha quantos {thing}s!"],
        count_rhyme=["{n} {thing}{s}! Um, dois, três!",
                     "{n} {thing}{s} para contar!",
                     "{n} {thing}{s}! Olha só!",
                     "Aqui tem um. Agora tem {n}!",
                     "{n} {thing}{s}, um por um!",
                     "Conte comigo! {n} {thing}{s}!",
                     "{n} {thing}{s}! Contar é lindo!"],
        count_outro=["Contamos {n} {thing}{s}. Muito bem!",
                     "Você contou tudo! Viva você!",
                     "{n} {thing}{s}! Já está!"],
        colors={"red": "vermelho", "blue": "azul", "yellow": "amarelo", "green": "verde",
                "orange": "laranja", "purple": "roxo", "pink": "rosa", "brown": "marrom"},
        color_line=["Eu vejo {c}, {c}, {c}! {C}, {c} e mais {c}!",
                    "Olha, é {c}! {C} como um brinquedo {c}!",
                    "{C}! {C}! Você consegue achar algo {c}?",
                    "Aqui está {c}. Você gosta de {c}? Eu gosto!"],
        clues=["Sou pequeno e redondo. Adoro brincar. Quem sou eu?",
               "Escute o meu som. Você consegue adivinhar? Quem sou eu?",
               "Eu tenho um segredo. Só você pode adivinhar. Quem sou eu?"],
        animal_names={"duck": "Pato", "frog": "Sapo", "cat": "Gato"},
        animals={"duck": ("Quá quá!", "Tenho um bico pequeno e dois pés laranjas."),
                 "frog": ("Ribbit ribbit!", "Eu pulo e salto e adoro o lago."),
                 "cat": ("Miau miau!", "Tenho bigodes macios e uma cauda que balança.")},
        lesson_names={"sharing": "Compartilhar", "trying again": "Tentar de Novo",
                      "being kind": "Ser Gentil", "saying sorry": "Pedir Desculpa",
                      "being brave": "Ser Corajoso", "sharing a snack": "Dividir o Lanche"},
        stories={
            "sharing": ["{c} encontra uma bola vermelha grande.", "Um amigo também quer brincar.",
                        "{c} rola a bola para o amigo.", "Dois amigos, uma bola, muita diversão!"],
            "trying again": ["{c} quer alcançar um balão bem alto.", "{c} pula uma vez. Ainda não!",
                             "{c} pula duas vezes. Quase!", "Mais um pulo. {c} conseguiu!"],
            "being kind": ["{c} vê um passarinho tristinho.", "{c} dá um sorriso quentinho.",
                           "{c} acena com a asa devagar.", "O passarinho sorri. A gentileza é linda!"],
            "saying sorry": ["{c} esbarra na torre do amigo. Ah, não!", "{c} pede desculpa baixinho.",
                             "Eles constroem de novo juntos.", "Dois ajudantes são melhores que um!"],
            "being brave": ["O céu fica um pouquinho escuro.", "{c} fica tímido e pequenino.",
                            "{c} respira fundo e com coragem.", "{c} é corajoso. Tudo está bem!"],
            "sharing a snack": ["{c} tem duas maçãs suculentas.", "Um amigo não tem nenhuma.",
                                "{c} dá uma maçã de presente.", "Dividir deixa a maçã mais doce!"],
        },
        beats=["{c} acorda. O sol está quentinho. Que dia lindo!",
               "{c} vê algo maravilhoso. Que bom!",
               "{c} está feliz. Nós podemos juntos!",
               "Boa noite, amiguinhos. Durmam bem!",
               "{c} respira fundo e tenta outra vez.",
               "Olha! {c} encontrou um amigo para ajudar."],
        thumbs={"counting": "CONTE COM PIP", "colors": "CAÇA ÀS CORES",
                "guess_animal": "QUEM SOU EU?", "story": "SEJA GENTIL"},
        titles={
            "counting": ["{c} conta {n} {things}", "Conte até {n} com {c}",
                         "{n} {things} com {c}", "O dia de contar de {c}"],
            "colors": ["{c} e as cores: {list}", "Aprenda cores com {c}: {list}",
                       "{c} encontra {list}"],
            "guess_animal": ["Quem sou eu? {animals}", "Adivinhe o animal: {animals}",
                             "Você consegue adivinhar? {animals}"],
            "story": ["{c} e {lesson}", "{c} aprende a {lesson}", "Uma história sobre {lesson}"],
        },
        blurb={"counting": "{c} conta {things}, um por um. Um vídeo calmo e alegre para pequenos.",
               "colors": "{c} procura cores e nomeia cada uma.",
               "guess_animal": "{c} brinca de adivinhar. Escute a pista e conheça o animal.",
               "story": "Uma história carinhosa sobre {lesson} para os pequenos, com {c}."},
        tags={"counting": ["contar para crianças", "números para bebês", "aprender a contar"],
              "colors": ["cores para crianças", "aprender cores", "vídeo de cores"],
              "guess_animal": ["adivinhe o animal", "sons de animais", "jogo para crianças"],
              "story": ["história para dormir", "histórias infantis", "histórias para crianças"]},
    ),
}

COUNT_THINGS = ["balloon", "apple", "star", "ball"]
COLOR_SETS = [["red", "blue", "yellow"], ["green", "orange", "purple"],
              ["pink", "blue", "brown"], ["yellow", "red", "green"],
              ["purple", "orange", "pink"], ["blue", "green", "yellow"]]
COLOR_HEX = {"red": "#E03B3B", "blue": "#3F8EE0", "yellow": "#FFC94A", "green": "#4FA83D",
             "orange": "#F58A2E", "purple": "#B267D9", "pink": "#F58AC4", "brown": "#8A5A32"}
THING_HEX = {"balloon": "#E03B3B", "apple": "#E03B3B", "star": "#FFD23F", "ball": "#F25C54"}
ANIMAL_KEYS = ["duck", "frog", "cat"]
LESSONS = ["sharing", "trying again", "being kind", "saying sorry",
           "being brave", "sharing a snack"]


def _props_for_thing(thing):
    """Map a counting noun to the scene prop the 3D studio can actually draw."""
    return {"balloon": "balloons", "apple": "apples", "star": "stars",
            "ball": "ball_color"}.get(thing, "stars")


def render_line(lang, line, char):
    """Turn a structured line into text in the given language."""
    p = LANG.get(lang, LANG["en"])
    k, i, params = line["k"], line.get("i", 0), dict(line.get("p") or {})
    params.setdefault("c", char)
    if "color_key" in params:
        cname = p["colors"][params["color_key"]]
        params["c"] = cname
        params["C"] = cname.capitalize()
    if "thing_key" in params:
        singular, plural, gender = p["things"][params["thing_key"]]
        params["thing"] = singular
        params["s"] = "s" if params.get("plural") else ""
        if "n_num" in params:
            n_num = params["n_num"]
            fem = gender == "f" and p.get("words_fem", {}).get(n_num)
            params["n"] = fem or p["words"].get(n_num, str(n_num))
    if k == "count_intro":
        return p["count_intro"][i % len(p["count_intro"])].format(**params)
    if k == "count_rhyme":
        return p["count_rhyme"][i % len(p["count_rhyme"])].format(**params)
    if k == "count_outro":
        return p["count_outro"][i % len(p["count_outro"])].format(**params)
    if k == "color":
        return p["color_line"][i % len(p["color_line"])].format(**params)
    if k == "clue":
        return p["clues"][i % len(p["clues"])]
    if k == "reveal":
        a = p["animals"][params["animal"]]
        return "%s %s" % (a[0], a[1])
    if k == "beat":
        return p["beats"][i % len(p["beats"])].format(**params)
    if k == "story":
        return p["stories"][params["lesson"]][i].format(**params)
    return ""


def _generate_once(cfg, kind, fmt):
    """One draw from the grammar. Emits structured lines, not finished strings."""
    n = cfg["short_scenes"] if fmt == "short" else cfg["long_scenes"]
    char = cfg["character_name"]
    en = LANG["en"]

    if kind == "counting":
        thing = random.choice(COUNT_THINGS)
        prop = _props_for_thing(thing)
        props_base = {"trees": random.randint(0, 2), "hills": 2, "sun": True}
        intro_i = random.randrange(len(en["count_intro"]))
        rhyme_i = random.randrange(len(en["count_rhyme"]))
        outro_i = random.randrange(len(en["count_outro"]))
        scenes = []
        for i in range(1, n + 1):
            if i == 1:
                key, idx = "count_intro", intro_i
            elif i == n:
                key, idx = "count_outro", outro_i
            else:
                key, idx = "count_rhyme", rhyme_i
            p = dict(props_base)
            if prop == "ball_color":
                p["ball_color"] = THING_HEX[thing]
                p["ball_spread"] = max(1, i)
            else:
                p[prop] = i
            scenes.append({
                "line": {"k": key, "i": idx,
                         "p": {"n_num": i, "thing_key": thing, "plural": i > 1}},
                "on_screen_text": str(i) if i > 1 else "",
                "props": p,
                "pose": {"arm_raise": round(0.2 + 0.15 * (i % 3), 2),
                         "head_tilt": 4 if i % 2 else -4},
            })
        title_shape = random.randrange(6)
        meta = {"shape": title_shape, "n": n, "thing": thing}

    elif kind == "colors":
        cols = random.sample(COLOR_SETS, 1)[0][:]
        scenes = []
        for i in range(n):
            c = cols[i % len(cols)]
            scenes.append({
                "line": {"k": "color", "i": random.randrange(len(en["color_line"])),
                         "p": {"color_key": c}},
                "on_screen_text": en["colors"][c],
                "props": {"apples": random.choice([2, 3]), "hills": random.randint(1, 2),
                          "trees": random.randint(0, 2), "sun": random.random() > 0.3},
                "pose": {"arm_raise": round(0.2 + 0.2 * (i % 3), 2), "head_tilt": 5},
            })
        meta = {"colors": cols}

    elif kind == "guess_animal":
        pool = ANIMAL_KEYS[:]
        random.shuffle(pool)
        pairs = max(1, n // 2)
        scenes = []
        for i in range(pairs):
            animal = pool[i % len(pool)]
            scenes.append({
                "line": {"k": "clue", "i": random.randrange(len(en["clues"]))},
                "on_screen_text": "?",
                "character": "pip",
                "props": {"bush": True, "hills": 2, "trees": random.randint(1, 2), "sun": True},
                "pose": {"head_tilt": 8, "arm_raise": 0.2},
            })
            scenes.append({
                "line": {"k": "reveal", "p": {"animal": animal}},
                "on_screen_text": animal,
                "character": animal,
                "props": {"hills": 2, "trees": random.randint(0, 1), "sun": True},
                "pose": {"hop": 0.8, "arm_raise": 0.7},
            })
        meta = {"animals": list(dict.fromkeys(pool[i % len(pool)] for i in range(pairs)))}

    else:  # story
        lesson = random.choice(LESSONS)
        beat_order = list(range(len(en["beats"])))
        random.shuffle(beat_order)
        scenes = []
        for i in range(n):
            if i < 4:
                line = {"k": "story", "p": {"lesson": lesson}, "i": i}
            else:
                line = {"k": "beat", "i": beat_order[(i - 4) % len(beat_order)]}
            scenes.append({"line": line, "on_screen_text": "",
                           "props": {"hills": random.randint(1, 2),
                                     "trees": random.randint(0, 2)},
                           "pose": {"arm_raise": round(0.2 + 0.2 * (i % 3), 2)}})
        meta = {"lesson": lesson}

    return {"kind": kind, "scenes": scenes, "meta": meta, "lang": "en"}


def localize(story, lang, char):
    """Render one story into another language. Same structure, same shots, same render."""
    out = json.loads(json.dumps(story))
    p = LANG.get(lang, LANG["en"])
    for sc in out["scenes"]:
        line = sc.get("line")
        sc["narration"] = render_line(lang, line, char) if line else sc.get("narration", "")
        if sc.get("on_screen_text"):
            t = sc["on_screen_text"].lower()
            if t in p["colors"]:
                sc["on_screen_text"] = p["colors"][t]      # colour word, translate it
            elif t in p["animals"]:
                sc["on_screen_text"] = t                   # animal key stays a key
            elif t == "?":
                pass
            elif t.isdigit():
                pass                                       # digits are universal
    out["lang"] = lang
    out.update(render_meta(story, lang, char))
    return out


def render_meta(story, lang, char):
    """Title / thumb / description / tags in the target language."""
    p = LANG.get(lang, LANG["en"])
    kind, meta = story["kind"], story["meta"]
    n = len(story["scenes"])

    if kind == "counting":
        things = p["things"][meta["thing"]][1]
        title = p["titles"]["counting"][meta["shape"] % len(p["titles"]["counting"])].format(
            c=char, n=p["words"].get(n, n), things=things[0].upper() + things[1:])
        blurb = p["blurb"]["counting"].format(c=char, things=things)
        thumb = p["thumbs"]["counting"]
    elif kind == "colors":
        names = [p["colors"][c] for c in meta["colors"]]
        listing = ", ".join(x.capitalize() for x in names)
        title = random.choice(p["titles"]["colors"]).format(c=char, list=listing)
        blurb = p["blurb"]["colors"].format(c=char)
        thumb = p["thumbs"]["colors"]
    elif kind == "guess_animal":
        cap = ", ".join(p["animal_names"][a] for a in meta["animals"])
        title = random.choice(p["titles"]["guess_animal"]).format(animals=cap)
        blurb = p["blurb"]["guess_animal"].format(c=char)
        thumb = p["thumbs"]["guess_animal"]
    else:
        lesson = p["lesson_names"][meta["lesson"]]
        title = random.choice(p["titles"]["story"]).format(c=char, lesson=lesson)
        blurb = p["blurb"]["story"].format(c=char, lesson=lesson.lower())
        thumb = p["thumbs"]["story"]

    return {"title": title[:90], "thumb_text": thumb, "description": blurb,
            "tags": p["tags"][kind][:], "lang_label": p["label"]}


def generate_script(cfg, kind, fmt, past, lang="en"):
    """Public entry. Builds the story, then renders it in `lang`."""
    char = cfg["character_name"]
    seen = set(past or [])
    last = None
    for _ in range(24):
        last = _generate_once(cfg, kind, fmt)
        cand = localize(last, lang, char)
        if cand["title"] not in seen:
            return cand
    return localize(last, lang, char)



# --------------------------------------------------------------------------- AI writer
# Gemini writes the script when a key is available; the grammar above is the fallback, so a
# bad API day degrades to "safe and repetitive" instead of "no video".
BANNED_WORDS = ("kill", "dead", "die", "scary", "monster", "blood", "hate", "stupid",
                "gun", "fight", "hurt", "ugly", "dumb")

PROMPT = """You write preschool videos for ages 2-5 on the channel "{channel}".
Main character: {character}.
Kind of video: {kind}.
Write exactly {n} scenes.

Rules:
- Very simple English a 3 year old knows. Each narration is 1-2 SHORT sentences (max 18 words).
- Gentle, warm, positive. Nothing scary, violent or sad-ending. No brand names or existing characters.
- Where it fits, use a gentle rhyme and repeat a catchy phrase.
- "on_screen_text" is a single word or number, or "" for nothing.
- The pictures are 3D renders with ONE character per scene on a grassy hill. Only this can be shown:
  character: "pip" (the star), "duck", "frog" or "cat" (guest friends, use sparingly);
  props: balloons (0-5), apples (0-5), stars (0-5), fish (0-4), hills (1-2), trees (0-3), sun (true/false),
  bush (true/false, Pip peeks over it); pose: arm_raise from 0.0 (arms down) to 1.0 (arms up, cheering).
- The narration MUST describe only what the picture shows. NEVER mention things that cannot be drawn
  (towers, houses, toys, food other than apples, other people, vehicles, weather). If a friend is part of
  the story, show the friend in their own scene using the character field.
- Vary pose and props from scene to scene so the pictures do not all look the same.

Return ONLY JSON, no markdown:
{{"title": "under 70 characters",
  "description": "2 warm sentences for parents",
  "tags": ["8-12 short tags"],
  "scenes": [{{"narration": "...", "on_screen_text": "", "character": "pip",
               "props": {{"hills": 2, "trees": 1, "balloons": 0}}, "pose": {{"arm_raise": 0.5}}}}]}}"""

KIND_BRIEF = {
    "counting": "A counting rhyme that counts up from 1, adding one object per scene.",
    "colors": "A colour hunt: each scene is about one colour and finds 2-3 things of it.",
    "guess_animal": "Alternating scenes: a clue that asks 'Who am I?', then the animal reveal.",
    "story": "A tiny story with a kind lesson: gentle problem, kind solution, happy ending.",
}


def _gemini_models(key):
    """Model names change and differ per key, so ask Google which ones this key can use."""
    import requests
    names = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key, "pageSize": 200}, timeout=60)
        r.raise_for_status()
        found = []
        for m in r.json().get("models", []):
            n = m["name"].split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(x in n for x in ("image", "tts", "live", "audio", "embed",
                                                          "robotics", "computer")):
                continue
            found.append(n)
        found.sort(key=lambda n: (("preview" in n or "exp" in n), "lite" in n,
                                  [-int(x) for x in re.findall(r"\d+", n)]))
        names += found
        print("  gemini models available: %s" % ", ".join(found[:4]))
    except Exception as e:
        print("  could not list gemini models: %s" % e)
    names += ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
    return list(dict.fromkeys(names))


def _gemini_json(prompt, key, temperature=0.95):
    """Call Gemini and parse JSON. Returns None on any problem."""
    import requests
    for model in _gemini_models(key):
        for attempt in range(2):
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
                    params={"key": key},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json",
                                               "temperature": temperature}},
                    timeout=90)
                if r.status_code in (429, 500, 503):
                    time.sleep(6 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    print("  gemini %s: HTTP %s" % (model, r.status_code))
                    break
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                return json.loads(text)
            except Exception as e:
                print("  gemini %s failed: %s" % (model, e))
                break
    return None


ALLOWED_CHARS = ("pip", "duck", "frog", "cat")
INT_PROPS = {"balloons": 5, "apples": 5, "stars": 5, "fish": 4, "hills": 2, "trees": 3}


def _sanitize_visuals(obj):
    """Gemini may only ask for things Blender can draw. Anything else is dropped."""
    for sc in obj.get("scenes", []) or []:
        who = str(sc.get("character", "pip")).lower()
        sc["character"] = who if who in ALLOWED_CHARS else "pip"
        raw = sc.get("props") if isinstance(sc.get("props"), dict) else {}
        props = {}
        for k, mx in INT_PROPS.items():
            if k in raw:
                try:
                    props[k] = max(0, min(mx, int(raw[k])))
                except (TypeError, ValueError):
                    pass
        props.setdefault("hills", 2)
        if raw.get("sun"):
            props["sun"] = True
        if raw.get("bush"):
            props["bush"] = True
        sc["props"] = props
        pose = sc.get("pose") if isinstance(sc.get("pose"), dict) else {}
        try:
            sc["pose"] = {"arm_raise": max(0.0, min(1.0, float(pose.get("arm_raise", 0.3))))}
        except (TypeError, ValueError):
            sc["pose"] = {"arm_raise": 0.3}


def gemini_script(cfg, kind, fmt, past):
    """Ask Gemini for a script. Returns None on any problem - caller falls back to grammar."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    n = cfg["short_scenes"] if fmt == "short" else cfg["long_scenes"]
    avoid = "; ".join((past or [])[-20:]) or "none"
    prompt = (PROMPT.format(channel=cfg["channel_name"], character=cfg["character_name"],
                            kind=KIND_BRIEF[kind], n=n)
              + "\nDo NOT reuse these earlier titles: " + avoid)
    obj = _gemini_json(prompt, key)
    if not obj:
        return None
    obj["kind"] = kind
    obj.setdefault("meta", {})
    obj["lang"] = "en"
    _sanitize_visuals(obj)
    return obj if validate_script(obj, n) else None


def validate_script(st, n):
    """Reject anything unsafe or malformed so it never reaches a render."""
    try:
        if not st.get("title") or not st.get("scenes"):
            return False
        if "description" not in st or not isinstance(st.get("tags"), list):
            return False
        if len(st["scenes"]) != n:
            print("  writer: expected %d scenes, got %d" % (n, len(st["scenes"])))
            return False
        for sc in st["scenes"]:
            nar = (sc.get("narration") or "").strip()
            if not nar or len(nar.split()) > 22:
                print("  writer: bad narration length:", repr(nar[:60]))
                return False
            low = nar.lower()
            if any(w in low for w in BANNED_WORDS):
                print("  writer: unsuitable wording:", nar[:60])
                return False
            # Blender scene props: give the shot planner something to draw
            if not sc.get("props"):
                pl = nar.lower()
                props = {"hills": 2, "sun": True}
                for word, prop in (("balloon", "balloons"), ("apple", "apples"),
                                   ("star", "stars"), ("ball", "ball_color")):
                    if word in pl:
                        props[prop] = 3 if prop != "ball_color" else 1
                        break
                sc["props"] = props
            sc.setdefault("pose", {})
            sc.setdefault("on_screen_text", "")
        st.setdefault("thumb_text", "HELLO")
        return True
    except Exception as e:
        print("  writer: validation error:", e)
        return False


def translate_story(story, lang, cfg):
    """Translate a text-based (AI-written) story. Falls back to English text if no key."""
    if lang == "en" or not os.environ.get("GEMINI_API_KEY"):
        return story
    out = json.loads(json.dumps(story))
    lines = [sc["narration"] for sc in out["scenes"]]
    prompt = ("Translate these preschool narration lines into %s. Keep them very simple and "
              "warm, max 18 words each, same number of lines. Also translate the title, "
              "description and thumbnail text. Return ONLY JSON: "
              '{"title": "...", "description": "...", "thumb_text": "...", '
              '"narration": ["...", "..."]}\n\nLINES:\n%s'
              % (LANG[lang]["label"], json.dumps({"title": story["title"],
                                                  "description": story["description"],
                                                  "thumb_text": story.get("thumb_text", ""),
                                                  "narration": lines}, ensure_ascii=False)))
    obj = _gemini_json(prompt, os.environ["GEMINI_API_KEY"], temperature=0.4)
    if not obj or len(obj.get("narration", [])) != len(lines):
        print("  translate %s failed; keeping English" % lang)
        return story
    for sc, txt in zip(out["scenes"], obj["narration"]):
        sc["narration"] = txt
    out["title"] = obj.get("title") or story["title"]
    out["description"] = obj.get("description") or story["description"]
    out["thumb_text"] = obj.get("thumb_text") or story.get("thumb_text", "")
    out["tags"] = LANG[lang]["tags"][story["kind"]][:]
    out["lang"] = lang
    return out


def write_script(cfg, kind, fmt, past):
    """Choose the writer. Returns (story, source)."""
    mode = str(cfg.get("writer", "auto")).lower()
    if mode in ("gemini", "auto"):
        st = gemini_script(cfg, kind, fmt, past)
        if st:
            print("  script written by Gemini")
            return st, "gemini"
        if mode == "gemini":
            print("  writer=gemini but the call failed; using the grammar")
    return generate_script(cfg, kind, fmt, past), "grammar"


# --------------------------------------------------------------------------- shot planning
def plan_shots(story, fmt, work):
    """Turn scenes into Blender shot specs (one still per scene)."""
    shots = []
    for i, sc in enumerate(story["scenes"]):
        props = dict(sc.get("props") or {})
        # map "stamp" (on-screen count) to the right visual prop family
        if "stamp" in props:
            c = props.pop("stamp")
            if "balloon" in str(props).lower() or not props.get("apples"):
                pass
        # mystery = hide the animal, reveal = show it
        if props.pop("mystery", None):
            props.setdefault("balloons", 0)
            props["trees"] = 2
        props.pop("reveal", None)
        sky, ground, back = "#CFEBFF", "#8FD653", "#EAF6FF"
        if "sky" in props:
            sky = props.pop("sky")
        cam = dict(sc.get("cam") or {})
        if fmt == "long":
            cam.setdefault("fill", 0.78)     # 16:9 needs less vertical breathing room
            cam.setdefault("bottom", 0.13)
        shots.append({
            "out": os.path.join(work, "shot_%02d.png" % i),
            "res": list(SHORT if fmt == "short" else LONG),
            "character": sc.get("character", "pip"),
            "kind": story["kind"],
            "props": props,
            "pose": sc.get("pose") or {},
            "sky": sky, "ground": ground, "back": back,
            "cam": cam,
        })
    return shots


# --------------------------------------------------------------------------- blender render
def render_shots(cfg, shots, work, proxy=False, q=None):
    """Render every shot in ONE Blender launch (saves ~4s startup each)."""
    if proxy:
        for sh in shots:
            make_proxy_still(sh["out"])
        return shots
    blender = find_blender(cfg)
    if not blender:
        raise RuntimeError("Blender not found. Set 'blender' in config.json or BLENDER=/path/to/blender")
    q = q or resolve_quality(cfg)
    spec = {"samples": int(q["samples"]), "supersample": float(q.get("supersample", 1.0)),
            "shots": shots}
    sp = os.path.join(work, "shots.json")
    json.dump(spec, open(sp, "w"), indent=1)
    t = time.time()
    r = subprocess.run([blender, "-b", "--factory-startup", "-P", STUDIO, "--", "--shots", sp],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = r.stdout.decode(errors="ignore")
    open(os.path.join(work, "blender.log"), "w").write(log)
    if r.returncode != 0 or "ALL SHOTS DONE" not in log:
        raise RuntimeError("Blender render failed:\n" + log[-1500:])
    print("  rendered %d shots in %.1fs" % (len(shots), time.time() - t))
    for sh in shots:
        if not os.path.exists(sh["out"]):
            raise RuntimeError("missing render: " + sh["out"])
    return shots


def make_proxy_still(path):
    """Flat placeholder still for --no-blender plumbing tests."""
    from PIL import Image, ImageDraw
    w, h = 864, 1536
    img = Image.new("RGB", (w, h), (207, 235, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, int(h * .72), w, h), fill=(143, 214, 83))
    d.ellipse((w * .32, h * .34, w * .68, h * .70), fill=(157, 190, 221))
    d.ellipse((w * .40, h * .26, w * .60, h * .44), fill=(157, 190, 221))
    d.ellipse((w * .43, h * .30, w * .57, h * .38), fill=(239, 122, 47))
    img.save(path, "PNG")


# --------------------------------------------------------------------------- lip-sync
def mouth_track(work, idx, size, info, spans, dur, fps):
    """Write a small RGBA PNG sequence of mouth shapes, one per mouth-frame.

    Returns (directory, pattern, x_offset, y_offset, fps) or None.
    Only the mouth patch is written, so each frame is a couple of KB.
    """
    from PIL import Image, ImageDraw
    if not spans or not info:
        return None
    w, h = size
    cx, cy = info["beak_x"] * w, info["beak_y"] * h
    mw = max(14.0, info.get("mouth_w", 0.09) * w)
    tw, th = int(mw * 2.2), int(mw * 1.9)
    x0, y0 = int(cx - tw / 2), int(cy - th / 2)
    x0 = max(0, min(w - tw, x0))
    y0 = max(0, min(h - th, y0))

    d = os.path.join(work, "mouth_%02d" % idx)
    os.makedirs(d, exist_ok=True)
    n = max(1, int(dur * fps))
    lip = (172, 68, 80, 255)
    lip_edge = (104, 40, 48, 255)
    tongue = (232, 138, 146, 255)
    for f in range(n):
        t = f / fps
        amp = 0.0
        for (a, b) in spans:
            if a - 0.02 <= t <= b + 0.02:
                amp = 0.55 + 0.45 * abs(math.sin((t - a) * 2 * math.pi * 5.0))
                break
        img = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        if amp > 0.05:
            dd = ImageDraw.Draw(img)
            bw = mw * 0.98
            bh = max(2.5, mw * 0.78 * amp)
            lx, ly = tw / 2 - bw / 2, th / 2 - bh / 2
            dd.ellipse((lx, ly, lx + bw, ly + bh), fill=lip,
                       outline=lip_edge, width=max(1, int(mw * 0.05)))
            dd.ellipse((lx + bw * 0.28, ly + bh * 0.42, lx + bw * 0.72, ly + bh * 0.98),
                       fill=tongue)
        img.save(os.path.join(d, "m_%03d.png" % f))
    return d, "m_%03d.png", x0, y0, fps


# --------------------------------------------------------------------------- 2.5D motion
def motion_clip(still, audio, overlay_png_, out, size, fps, dur, idx, mouth=None, q=None, caps=None):
    """Animate one still: push-in / pan / parallax-ish drift + a gentle character bob."""
    w, h = size
    frames = max(1, int(dur * fps))
    kinds = [
        # slow push in, centred
        ("min(zoom+0.0009,1.16)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        # pull out
        ("if(eq(on,0),1.16,max(zoom-0.0009,1.0))", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
        # drift right
        ("1.12", "(iw-iw/zoom)*on/%d" % frames, "ih/2-(ih/zoom/2)"),
        # drift left
        ("1.12", "(iw-iw/zoom)*(1-on/%d)" % frames, "ih/2-(ih/zoom/2)"),
        # slight tilt-lift push
        ("min(zoom+0.0007,1.10)", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-on/%d)" % frames),
    ]
    z, x, y = kinds[idx % len(kinds)]
    fc = ("[0:v]scale=%d:%d,zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d[bg];"
          % (w * 2, h * 2, z, x, y, frames, w, h, fps))
    parts = []
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", still]
    q = q or QUALITY["high"]
    # the still was rendered above delivery size; Lanczos on the way down is what makes it crisp
    parts.append("[0:v]scale=%d:%d:flags=lanczos,zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d[bg]"
                 % (w * 2, h * 2, z, x, y, frames, w, h, fps))
    idx_in, cur = 1, "[bg]"
    if overlay_png_:
        cmd += ["-loop", "1", "-framerate", str(fps), "-t", "%.2f" % dur, "-i", overlay_png_]
        parts.append("[%d:v]format=rgba,fade=in:st=0.25:d=0.30:alpha=1[ov]" % idx_in)
        parts.append("%s[ov]overlay=0:0[vg]" % cur)
        cur, idx_in = "[vg]", idx_in + 1
    if mouth:
        mdir, mpat, mx, my, mdir_fps = mouth
        cmd += ["-framerate", "%d" % mdir_fps, "-start_number", "0",
                "-i", os.path.join(mdir, mpat)]
        parts.append("[%d:v]format=rgba[mo]" % idx_in)
        parts.append("%s[mo]overlay=%d:%d:eof_action=repeat[vm]" % (cur, mx, my))
        cur, idx_in = "[vm]", idx_in + 1
    if caps:
        cmd += ["-f", "concat", "-safe", "0", "-i", caps]
        parts.append("[%d:v]format=rgba,fps=%d[cp]" % (idx_in, fps))
        parts.append("%s[cp]overlay=0:0:format=auto[vc]" % cur)
        cur, idx_in = "[vc]", idx_in + 1
    parts.append("%sformat=yuv420p[v]" % cur)
    fc = ";".join(parts)
    ai = idx_in
    cmd += ["-i", audio, "-filter_complex", fc, "-map", "[v]", "-map", "%d:a" % ai,
            "-af", "adelay=%d:all=1,apad=whole_dur=%.2f" % (int(0.35 * 1000), dur),
            "-t", "%.2f" % dur, "-c:v", "libx264", "-preset", q["preset"], "-crf", str(q["crf"]),
            "-tune", "animation", "-x264-params", "aq-mode=3:psy-rd=1.0,0.15",
            "-r", str(fps), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-ar", "48000", "-ac", "2", out]
    run(cmd)
    return dur


# --------------------------------------------------------------------------- text overlay
def font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


COLOR_WORDS = {"red": (231, 40, 40), "blue": (30, 110, 230), "yellow": (240, 180, 0),
               "green": (30, 160, 70), "orange": (245, 130, 20), "purple": (140, 60, 200),
               "pink": (240, 80, 150), "brown": (140, 90, 50)}


def overlay_png(text, size, path, idx):
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base = h * 0.085 if h > w else h * 0.13
    if len(text.strip()) <= 3:
        base = h * 0.155 if h > w else h * 0.24
    fs = int(base)
    while True:
        f = font(fs)
        box = d.textbbox((0, 0), text, font=f, stroke_width=max(4, fs // 14))
        if box[2] - box[0] <= w * 0.80 or fs < 36:
            break
        fs = int(fs * 0.9)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = int(fs * 0.34)
    cx, cy = w // 2, int(h * 0.115) + th // 2
    d.rounded_rectangle((cx - tw // 2 - pad, cy - th // 2 - pad, cx + tw // 2 + pad, cy + th // 2 + pad),
                        radius=int(fs * 0.5), fill=(255, 255, 255, 236),
                        outline=(34, 49, 74, 255), width=max(4, fs // 16))
    col = COLOR_WORDS.get(text.strip().lower(), (231, 76, 60))
    d.text((cx - tw // 2 - box[0], cy - th // 2 - box[1]), text, font=f, fill=col + (255,),
           stroke_width=max(3, fs // 20), stroke_fill=(34, 49, 74, 255))
    img.save(path, "PNG")
    return path


# --------------------------------------------------------------------------- voice
def estimate_spans(text, dur):
    """Fallback word spans when we have no real timings: weight words by length."""
    words = [w for w in text.split() if w.strip()]
    if not words:
        return []
    weights = [len(w) + 1 for w in words]
    total = sum(weights)
    spans, t = [], 0.0
    pad = 0.12
    usable = max(0.2, dur - 2 * pad)
    for w in weights:
        d = usable * w / total
        spans.append((t + pad, t + pad + d))
        t += d
    return spans


# --------------------------------------------------------------------------- word-by-word captions
def _caption_frame(chunk, active, size, path):
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fs = int(h * 0.050 if h > w else h * 0.078)
    while True:
        f = font(fs)
        sw = max(4, fs // 9)
        space = d.textlength(" ", font=f)
        widths = [d.textlength(x, font=f) for x in chunk]
        total = sum(widths) + space * (len(chunk) - 1)
        if total <= w * 0.90 or fs < 24:
            break
        fs = int(fs * 0.92)
    x = (w - total) / 2
    y = int(h * (0.70 if h > w else 0.80))     # above the Shorts buttons
    for i, word in enumerate(chunk):
        col = (255, 214, 10, 255) if i == active else (255, 255, 255, 255)
        d.text((x, y), word, font=f, fill=col, stroke_width=sw, stroke_fill=(25, 30, 60, 255))
        x += widths[i] + space
    img.save(path, "PNG")


def make_captions(words, spans, size, work, idx, dur, lead=0.35):
    """Returns a concat-list file for a transparent caption stream, or None."""
    from PIL import Image
    if not words or len(words) != len(spans):
        return None
    items = [(re.sub(r"\s+", " ", t).strip(), a + lead, b + lead) for t, (a, b) in zip(words, spans)]
    items = [x for x in items if x[0]]
    if not items:
        return None
    chunks = [items[i:i + 4] for i in range(0, len(items), 4)]
    blank = os.path.join(work, "cap_blank.png")
    if not os.path.exists(blank):
        Image.new("RGBA", size, (0, 0, 0, 0)).save(blank)
    lines, t = [], 0.0
    for ci, ch in enumerate(chunks):
        texts = [x[0] for x in ch]
        for wi, (_, a, b) in enumerate(ch):
            if wi + 1 < len(ch):
                end = ch[wi + 1][1]
            elif ci + 1 < len(chunks):
                end = chunks[ci + 1][0][1]
            else:
                end = min(dur, b + 0.5)
            if a > t:
                lines.append((blank, a - t))
            fr = os.path.join(work, "cap%02d_%d_%d.png" % (idx, ci, wi))
            _caption_frame(texts, wi, size, fr)
            seg = max(0.05, end - max(a, t))
            lines.append((fr, seg))
            t = max(t, a) + seg
    if t < dur:
        lines.append((blank, dur - t))
    lst = os.path.join(work, "cap%02d.txt" % idx)
    with open(lst, "w") as f:
        for fr, dd in lines:
            f.write("file '%s'\nduration %.3f\n" % (os.path.abspath(fr), dd))
        f.write("file '%s'\n" % os.path.abspath(lines[-1][0]))
    return lst


def make_voice(cfg, text, path, offline=False):
    """Synthesise speech and return word spans [(start_s, end_s), ...] for lip-sync."""
    if offline:
        secs = max(2.0, len(text) / 14)
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=300:sample_rate=24000", "-af", "volume=0.05",
             "-t", "%.2f" % secs, "-c:a", "libmp3lame", path])
        make_voice.words = [w for w in text.split() if w.strip()]
        return estimate_spans(text, secs)

    import asyncio

    import edge_tts

    async def go():
        # boundary="WordBoundary" is required; the default only emits sentence boundaries
        comm = edge_tts.Communicate(text, cfg["voice"], rate=cfg["voice_rate"],
                                    pitch=cfg["voice_pitch"], boundary="WordBoundary")
        spans, words = [], []
        with open(path, "wb") as fh:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    fh.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    a = chunk["offset"] / 1e7
                    spans.append((a, a + chunk["duration"] / 1e7))
                    words.append(str(chunk.get("text", "")))
        make_voice.words = words
        return spans

    last = None
    for attempt in range(4):
        try:
            spans = asyncio.run(go())
            if os.path.exists(path) and os.path.getsize(path) > 500:
                if not spans:
                    make_voice.words = [w for w in text.split() if w.strip()]
                return spans or estimate_spans(text, probe(path))
        except Exception as e:
            last = e
        time.sleep(4 * (attempt + 1))
    raise RuntimeError("voice failed after 4 tries: %s" % last)


# --------------------------------------------------------------------------- music
SCALE = [523.25, 587.33, 659.25, 783.99, 880.00, 1046.50]
BASS = [130.81, 110.00, 87.31, 98.00]


def make_music(path, seconds, seed):
    """Original 100 bpm pentatonic bed, synthesized. Royalty-free by construction."""
    from array import array
    import wave
    sr = 44100
    rnd = random.Random(seed)
    total = int(sr * (seconds + 1))
    buf = [0.0] * total
    cache = {}

    def note(freq, dur):
        key = (round(freq, 2), round(dur, 2))
        if key in cache:
            return cache[key]
        n = int(sr * dur)
        out = []
        for i in range(n):
            t = i / sr
            env = math.exp(-t * 5.5) * min(1.0, t * 220)
            v = (math.sin(2 * math.pi * freq * t)
                 + 0.32 * math.sin(2 * math.pi * freq * 4 * t) * math.exp(-t * 14)
                 + 0.13 * math.sin(2 * math.pi * freq * 2 * t))
            out.append(v * env)
        cache[key] = out
        return out

    def add(freq, start, dur, amp):
        s = int(start * sr)
        for i, v in enumerate(note(freq, dur)):
            j = s + i
            if j >= total:
                break
            buf[j] += v * amp

    beat = 60 / 100.0
    phrase = [rnd.randrange(len(SCALE)) for _ in range(8)]
    bar, t, b = beat * 4, 0.0, 0
    while t < seconds:
        add(BASS[b % 4], t, 1.6, 0.5)
        add(BASS[b % 4] * 2, t + beat * 2, 1.1, 0.26)
        for q in range(4):
            idx = phrase[(b * 4 + q) % 8]
            if (b // 4) % 2 == 1 and q == 3:
                idx = min(len(SCALE) - 1, idx + 1)
            if rnd.random() < 0.85:
                add(SCALE[idx], t + q * beat, 0.9, 0.40)
        t += bar
        b += 1
    peak = max(abs(x) for x in buf) or 1.0
    scale = 0.8 * 32767 / peak
    fade = int(sr * 2)
    data = array("h")
    for i, v in enumerate(buf):
        g = 1.0 if i <= total - fade else max(0.0, (total - i) / fade)
        s = int(v * scale * g)
        data.append(s)
        data.append(s)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(data.tobytes())
    return path


# --------------------------------------------------------------------------- assembly
XF = 0.30
TRANSITIONS = ["fadeblack"]      # a dissolve shows two Pips on top of each other


def _xfade_pair(a, b, dur_a, dur_b, out, crf, preset):
    """Crossfade two clips into one. Bounded memory: only two streams open at a time."""
    off = max(0.0, dur_a - XF)
    fc = ("[0:v][1:v]xfade=transition=fadeblack:duration=%.2f:offset=%.3f[v];"
          "[0:a][1:a]acrossfade=d=%.2f[a]" % (XF, off, XF))
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", a, "-i", b,
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
         "-tune", "animation", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out])


def join_crossfades(clips, durs, out, q=None):
    """Join every clip with crossfades.

    Two strategies: one filter graph (fast, but 12 inputs x 1080p needs a lot of RAM),
    or progressive pairwise merging (bounded memory, one extra encode generation).
    The choice is made from the number of clips and the RAM actually available, because
    the single-pass version gets OOM-killed on a small box and takes the whole run down.
    """
    q = q or QUALITY["high"]
    if len(clips) == 1:
        shutil.copy(clips[0], out)
        return

    avail_gb = free_ram_gb()
    single_pass_ok = len(clips) <= 6 and avail_gb >= 6.0

    if single_pass_ok:
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for c in clips:
            cmd += ["-i", c]
        fc, vp, ap, acc = [], "[0:v]", "[0:a]", durs[0]
        for i in range(1, len(clips)):
            vo, ao = "[v%d]" % i, "[a%d]" % i
            fc.append("%s[%d:v]xfade=transition=%s:duration=%.2f:offset=%.3f%s"
                      % (vp, i, TRANSITIONS[i % len(TRANSITIONS)], XF, acc - XF, vo))
            fc.append("%s[%d:a]acrossfade=d=%.2f%s" % (ap, i, XF, ao))
            vp, ap = vo, ao
            acc = acc + durs[i] - XF
        cmd += ["-filter_complex", ";".join(fc), "-map", vp, "-map", ap,
                "-c:v", "libx264", "-preset", q["preset"], "-crf", str(q["crf"]),
                "-tune", "animation", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out]
        run(cmd)
        return

    print("  joining progressively (%d clips, %.1f GB free)" % (len(clips), avail_gb))
    work = os.path.dirname(out)
    cur, cur_dur = clips[0], durs[0]
    for i in range(1, len(clips)):
        last = i == len(clips) - 1
        nxt = out if last else os.path.join(work, "xf_%02d.mp4" % i)
        crf = q["crf"] if last else 12          # near-lossless intermediates
        _xfade_pair(cur, clips[i], cur_dur, durs[i], nxt, crf, q["preset"])
        if cur != clips[0] and os.path.exists(cur):
            os.remove(cur)
        cur = nxt
        cur_dur = cur_dur + durs[i] - XF


def mix_music(video, music, out):
    total = probe(video)
    fc = ("[1:a]volume=0.17,afade=t=in:d=1.2,afade=t=out:st=%.2f:d=2.0[m];"
          "[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
          "loudnorm=I=-16:TP=-1.5:LRA=11[a]" % max(0, total - 2.2))
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", music,
         "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", out])


def thumbnail(still, text, size, out):
    from PIL import Image, ImageDraw, ImageOps
    img = ImageOps.fit(Image.open(still).convert("RGB"), size, Image.LANCZOS)
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 214, 10), width=max(10, w // 60))
    words = (text or "HELLO").upper().split()
    fs = int(h * (0.09 if h > w else 0.16))
    maxw = w * 0.86
    while True:
        f = font(fs)
        sw = max(5, fs // 9)
        lines, cur = [], ""
        for wd in words:
            trial = (cur + " " + wd).strip()
            if d.textbbox((0, 0), trial, font=f, stroke_width=sw)[2] <= maxw or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = wd
        lines.append(cur)
        widest = max(d.textbbox((0, 0), ln, font=f, stroke_width=sw)[2] for ln in lines)
        if (widest <= maxw and len(lines) <= 2) or fs < 30:
            break
        fs = int(fs * 0.9)
    y = int(h * 0.045)
    for ln in lines[:2]:
        d.text((w // 2, y), ln, font=f, fill=(255, 255, 255), anchor="ma",
               stroke_width=sw, stroke_fill=(30, 40, 70))
        y += int(fs * 1.18)
    img.save(out, "JPEG", quality=92)


def check_video(path, fmt, offline=False):
    info = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", "json", path]))["streams"]
    types = [s["codec_type"] for s in info]
    dur = probe(path)
    problems = []
    if "video" not in types:
        problems.append("no video stream")
    if "audio" not in types:
        problems.append("no audio stream")
    if offline:
        return problems, dur
    v = next((s for s in info if s["codec_type"] == "video"), {})
    want = SHORT if fmt == "short" else LONG
    if (v.get("width"), v.get("height")) != want:
        problems.append("wrong resolution %sx%s, expected %dx%d"
                        % (v.get("width"), v.get("height"), *want))
    if fmt == "short" and not (15 <= dur <= 180):
        problems.append("short length %.0fs outside 15-180s" % dur)
    if fmt == "long" and dur < 60:
        problems.append("long video too short (%.0fs)" % dur)
    if os.path.getsize(path) < 200_000:
        problems.append("file is tiny")
    # audio must not be silent
    r = subprocess.run(["ffmpeg", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    err = r.stderr.decode(errors="ignore")
    for line in err.splitlines():
        if "mean_volume:" in line:
            try:
                mv = float(line.split("mean_volume:")[1].split("dB")[0])
                if mv < -45:
                    problems.append("audio is nearly silent (%.0f dB)" % mv)
            except Exception:
                pass
    return problems, dur


# --------------------------------------------------------------------------- upload
def check_credentials():
    """Fail in 5 seconds, not after a 40-minute render."""
    need = ["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise RuntimeError("missing secrets: " + ", ".join(missing))
    import requests
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("YouTube token refresh failed (%d): %s -- re-authorise if the OAuth "
                           "consent screen is in Testing (tokens expire after 7 days)"
                           % (r.status_code, r.text[:200]))


def build_upload_body(story, cfg, fmt, lang="en"):
    """Build the videos.insert request body. Pure function - unit tested."""
    title = story["title"][:95] + (" #Shorts" if fmt == "short" else "")
    desc = (story.get("description") or ""
            + "\n\nOriginal 3D characters, story and music, made by " + cfg["channel_name"]
            + ". Animation and voice are created with AI-assisted tools."
            + "\n#kids #learning #toddler")
    tags = [t for t in story["tags"] if t][:15]
    tag = LANG.get(lang, LANG["en"])["lang_tag"]
    # title/description must be in the video's own language for YouTube to index them
    return {"snippet": {"title": title[:100],
                        "description": desc[:5000], "tags": tags,
                        "categoryId": "27", "defaultLanguage": tag,
                        "defaultAudioLanguage": tag},
            "status": {"privacyStatus": cfg["privacy"],
                       "selfDeclaredMadeForKids": bool(cfg["made_for_kids"]),
                       "containsSyntheticMedia": True}}


def upload(video, thumb, story, cfg, fmt, lang="en"):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    body = build_upload_body(story, cfg, fmt, lang=lang)
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(video, chunksize=8 * 1024 * 1024,
                                                        resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
    except Exception as e:
        print("thumbnail skipped:", e)
    return vid


# --------------------------------------------------------------------------- main
def pick_kind(cfg, history, wanted):
    if wanted:
        return wanted
    used = [h.get("kind") for h in history[-12:]]
    return min(cfg["kinds"], key=lambda k: (used.count(k), random.random()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=["short", "long"], default="short")
    ap.add_argument("--kind", choices=["counting", "colors", "guess_animal", "story"])
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="render and check, never upload")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--test", action="store_true", help="short offline run")
    ap.add_argument("--no-blender", action="store_true", help="proxy stills (plumbing test)")
    ap.add_argument("--quality", choices=list(QUALITY), help="override config quality preset")
    ap.add_argument("--languages", help="comma separated, e.g. en,es,pt (overrides config)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--check-credentials", action="store_true",
                    help="only verify YouTube secrets, then exit")
    a = ap.parse_args()

    if a.check_credentials:
        try:
            check_credentials()
            print("credentials OK")
        except Exception as e:
            print("credentials FAILED:", e)
            sys.exit(1)
        return

    cfg = load_cfg()
    if a.quality:
        cfg["quality"] = a.quality
    langs = ([x.strip() for x in a.languages.split(",") if x.strip()]
             if a.languages else list(cfg.get("languages") or ["en"]))
    unknown = [x for x in langs if x not in LANG]
    if unknown:
        raise SystemExit("unknown language(s): %s -- available: %s"
                         % (", ".join(unknown), ", ".join(LANG)))
    q = resolve_quality(cfg)
    print("  quality: %s  (render %.2fx, %d spp, crf %s, denoise %s)"
          % (q["name"], q["supersample"], q["samples"], q["crf"], q["denoise"]))
    if a.seed is not None:
        random.seed(a.seed)
    history = json.load(open(HISTORY)) if os.path.exists(HISTORY) else []
    kind = pick_kind(cfg, history, a.kind)
    os.environ["BLENDER_DENOISE"] = "1" if q["denoise"] else "0"
    offline = a.test or a.no_blender        # voice/music/QA leniency
    art_proxy = a.no_blender                 # only this skips Blender
    if a.test:
        cfg["short_scenes"] = 3

    run_id = time.strftime("%Y%m%d-%H%M%S")
    work = os.path.join(HERE, "output", run_id)
    os.makedirs(work, exist_ok=True)
    size = SHORT if a.format == "short" else LONG

    if a.upload and not offline:
        print("[0/7] Checking credentials")
        check_credentials()

    print("[1/7] Script: %s, %s" % (kind, a.format))
    base, source = write_script(cfg, kind, a.format, [h.get("title") for h in history])
    print("  writer: %s   languages: %s" % (source, ", ".join(langs)))
    def _for_lang(lg):
        if lg == "en":
            return base
        if base.get("scenes") and base["scenes"][0].get("line"):
            return localize(base, lg, cfg["character_name"])   # grammar: exact, free
        return translate_story(base, lg, cfg)                  # AI text: needs translation

    stories = {lg: _for_lang(lg) for lg in langs}
    story = stories[langs[0]]
    json.dump(stories, open(os.path.join(work, "stories.json"), "w"), indent=2, ensure_ascii=False)
    if a.plan_only:
        for lg in langs:
            print("\n--- %s (%s) ---" % (lg, LANG[lg]["label"]))
            print(json.dumps(stories[lg], indent=2, ensure_ascii=False))
        return

    print("[2/7] Shot plan")
    shots = plan_shots(story, a.format, work)
    json.dump(shots, open(os.path.join(work, "plan.json"), "w"), indent=1)

    print("[3/7] 3D stills (%d shots, %s spp at %.2fx)"
          % (len(shots), q["samples"], q["supersample"]))
    render_shots(cfg, shots, work, proxy=art_proxy, q=q)

    print("[4/7] Motion + voice")
    outputs = []
    for lg in langs:
        outputs.append(produce_language(cfg, q, stories[lg], lg, shots, work, size, a, offline))
    problems = []
    for out, thumb, st, lg, dur in outputs:
        print("  %-3s %5.1fs  %s" % (lg, dur, os.path.basename(out)))
    probs_any = [p for p in problems]
    if probs_any:
        sys.exit("STOPPED: " + "; ".join(probs_any))

    if a.upload and not a.dry_run and not offline:
        print("[+] Upload (%s)" % cfg["privacy"])
        for out, thumb, st, lg, dur in outputs:
            vid = upload(out, thumb, st, cfg, a.format, lang=lg)
            print("  %-3s https://studio.youtube.com/video/%s/edit" % (lg, vid))
            history.append({"title": st["title"], "kind": kind, "id": vid,
                            "date": run_id, "format": a.format, "seconds": round(dur, 1),
                            "writer": source, "lang": lg})
        json.dump(history, open(HISTORY, "w"), indent=2, ensure_ascii=False)
    else:
        print("[+] Not uploading (dry run / offline)")
    return


def produce_language(cfg, q, story, lang, shots, work, size, args, offline):
    """Voice, overlays, clips, join, music and thumbnail for ONE language.

    The 3D stills are shared: everything language-specific happens after the render.
    """
    d = os.path.join(work, "lang_%s" % lang)
    os.makedirs(d, exist_ok=True)
    scenes = story["scenes"]
    outro_line = cfg["outro_line"] if lang == "en" else \
        cfg.get("outro_line_local", {}).get(lang, cfg["outro_line"])
    outro_card = "See you soon!" if lang == "en" else \
        cfg.get("outro_card_local", {}).get(lang, "See you soon!")
    outro = {"narration": outro_line, "on_screen_text": outro_card, "props": {}}
    work_items = [(shots[i], scenes[i]) for i in range(len(shots))] + [(shots[-1], outro)]
    clips, durs = [], []
    for i, (sh, sc) in enumerate(work_items):
        aud = os.path.join(d, "a%02d.mp3" % i)
        spans = make_voice(cfg, sc["narration"], aud, offline)
        ov = None
        if sc.get("on_screen_text"):
            ov = overlay_png(sc["on_screen_text"], size, os.path.join(d, "t%02d.png" % i), i)
        dur = max(2.6, probe(aud) + 0.35 + 0.75)
        mouth = None
        if cfg.get("lipsync", True):
            info_path = sh["out"] + ".json"
            info = json.load(open(info_path)) if os.path.exists(info_path) else None
            if info and spans:
                # voice starts after the 0.35 s lead-in added in motion_clip
                shifted = [(a + 0.35, b + 0.35) for (a, b) in spans]
                mouth = mouth_track(d, i, size, info, shifted, dur,
                                    int(q.get("mouth_fps", 30)))
        caps = None
        if cfg.get("captions", True):
            try:
                caps = make_captions(getattr(make_voice, "words", []), spans, size, d, i, dur)
            except Exception as e:
                print("  captions skipped:", e)
        clip = os.path.join(d, "c%02d.mp4" % i)
        motion_clip(sh["out"], aud, ov, clip, size, cfg["fps"], dur, i, mouth=mouth, q=q, caps=caps)
        clips.append(clip)
        durs.append(dur)
        if mouth:
            shutil.rmtree(mouth[0], ignore_errors=True)

    joined = os.path.join(d, "joined.mp4")
    join_crossfades(clips, durs, joined, q)

    out = os.path.join(d, "video_%s.mp4" % lang)
    if cfg["music"]:
        music = make_music(os.path.join(d, "music.wav"), probe(joined), random.randint(1, 10**6))
        mix_music(joined, music, out)
    else:
        shutil.copy(joined, out)

    thumb = os.path.join(d, "thumb_%s.jpg" % lang)
    thumbnail(shots[0]["out"], story.get("thumb_text") or "HELLO", size, thumb)
    problems, dur = check_video(out, args.format, offline)
    if problems:
        print("  %s FAILED quality check: %s" % (lang, "; ".join(problems)))
    print("  %-3s length %.1fs, problems: %s" % (lang, dur, problems or "none"))
    return out, thumb, story, lang, dur


if __name__ == "__main__":
    main()
