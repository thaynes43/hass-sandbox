"""Style variety for image generation.

Two-layer model:
- StyleProfile: rendering style (cartoon, watercolor, pixel art, etc.)
- EnvironmentVariant: scene/theme mutation (underwater, space, seasonal, etc.)

Both layers are randomly selected per image generation to add variety.
A "default" entry with empty prompt_suffix exists in each pool so that
some generations get no style/variant overlay — this is intentional variety.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StyleProfile:
    """Rendering style overlay."""

    id: str
    prompt_suffix: str
    description: str = ""


@dataclass(frozen=True)
class EnvironmentVariant:
    """Scene/theme mutation."""

    id: str
    prompt_suffix: str
    description: str = ""


STYLE_PROFILES: dict[str, StyleProfile] = {
    "default": StyleProfile(
        id="default",
        prompt_suffix="",
        description="No style overlay",
    ),
    "cartoon": StyleProfile(
        id="cartoon",
        prompt_suffix="Render in a fun cartoon style with bold outlines and bright colors.",
        description="Cartoon-style rendering",
    ),
    "watercolor": StyleProfile(
        id="watercolor",
        prompt_suffix="Render as a soft watercolor painting with flowing washes of color and gentle edges.",
        description="Watercolor painting",
    ),
    "pixel-art": StyleProfile(
        id="pixel-art",
        prompt_suffix="Render as retro pixel art in the style of a 16-bit video game.",
        description="Retro pixel art",
    ),
    "comic-book": StyleProfile(
        id="comic-book",
        prompt_suffix="Render in a comic book style with bold ink outlines, halftone shading, and dynamic composition.",
        description="Comic book style",
    ),
    "oil-painting": StyleProfile(
        id="oil-painting",
        prompt_suffix="Render as a rich oil painting with visible brushstrokes and warm, layered colors.",
        description="Oil painting",
    ),
    "anime": StyleProfile(
        id="anime",
        prompt_suffix="Render in an anime/manga style with large expressive eyes and clean linework.",
        description="Anime/manga style",
    ),
    "stained-glass": StyleProfile(
        id="stained-glass",
        prompt_suffix="Render as a stained glass window with bold colored segments separated by dark lead lines.",
        description="Stained glass",
    ),
    "pencil-sketch": StyleProfile(
        id="pencil-sketch",
        prompt_suffix="Render as a detailed pencil sketch with cross-hatching and graphite shading on white paper.",
        description="Pencil sketch",
    ),
    "pop-art": StyleProfile(
        id="pop-art",
        prompt_suffix="Render in a bold pop art style inspired by Warhol and Lichtenstein with saturated flat colors and Ben-Day dots.",
        description="Pop art",
    ),
    "claymation": StyleProfile(
        id="claymation",
        prompt_suffix="Render as if the scene were a claymation/stop-motion set with sculpted clay figures and miniature props.",
        description="Claymation/stop-motion",
    ),
    "impressionist": StyleProfile(
        id="impressionist",
        prompt_suffix="Render in an impressionist style with dappled light, visible brushwork, and a dreamy atmosphere.",
        description="Impressionist painting",
    ),
    "noir": StyleProfile(
        id="noir",
        prompt_suffix="Render in a film noir style with dramatic high-contrast black and white lighting and deep shadows.",
        description="Film noir",
    ),
    "art-deco": StyleProfile(
        id="art-deco",
        prompt_suffix="Render in an art deco style with geometric patterns, metallic accents, and elegant symmetry.",
        description="Art deco",
    ),
    "ukiyo-e": StyleProfile(
        id="ukiyo-e",
        prompt_suffix="Render in the style of Japanese ukiyo-e woodblock prints with flat color areas and flowing lines.",
        description="Japanese woodblock print",
    ),
    "low-poly": StyleProfile(
        id="low-poly",
        prompt_suffix="Render as low-poly 3D art with flat-shaded geometric triangles and a modern minimalist look.",
        description="Low-poly 3D",
    ),
    "paper-cutout": StyleProfile(
        id="paper-cutout",
        prompt_suffix="Render as a layered paper cutout collage with visible paper textures and subtle shadows between layers.",
        description="Paper cutout collage",
    ),
    "graffiti": StyleProfile(
        id="graffiti",
        prompt_suffix="Render as vibrant street art graffiti on a concrete wall with spray paint drips and bold lettering.",
        description="Street art graffiti",
    ),
    "children-book": StyleProfile(
        id="children-book",
        prompt_suffix="Render in a whimsical children's book illustration style with warm colors, soft textures, and a gentle, inviting feel.",
        description="Children's book illustration",
    ),
    "mosaic": StyleProfile(
        id="mosaic",
        prompt_suffix="Render as a tile mosaic with small colored tiles forming the image, like a Roman or Byzantine mosaic.",
        description="Tile mosaic",
    ),
    "neon": StyleProfile(
        id="neon",
        prompt_suffix="Render with glowing neon outlines against a dark background, like neon signs at night.",
        description="Neon glow",
    ),
    "isometric": StyleProfile(
        id="isometric",
        prompt_suffix="Render as an isometric diorama viewed from a 30-degree angle with clean edges and miniature-world charm.",
        description="Isometric diorama",
    ),
    "chalk": StyleProfile(
        id="chalk",
        prompt_suffix="Render as colorful chalk art drawn on a dark chalkboard with smudged edges and dusty textures.",
        description="Chalk art",
    ),
    "sticker": StyleProfile(
        id="sticker",
        prompt_suffix="Render as a cute die-cut sticker with a white border, glossy finish, and kawaii proportions.",
        description="Die-cut sticker",
    ),
    "vintage-photo": StyleProfile(
        id="vintage-photo",
        prompt_suffix="Render as a faded vintage photograph with sepia tones, film grain, and slightly soft focus.",
        description="Vintage photograph",
    ),
    "hyperrealistic": StyleProfile(
        id="hyperrealistic",
        prompt_suffix="Render in a hyperrealistic photographic style with lifelike textures, natural lighting, and extremely fine detail.",
        description="Hyperrealistic photo",
    ),
    "cinematic": StyleProfile(
        id="cinematic",
        prompt_suffix="Render in a cinematic style with dramatic lighting, rich color grading, shallow depth of field, and blockbuster polish.",
        description="Cinematic film still",
    ),
    "storybook-fantasy": StyleProfile(
        id="storybook-fantasy",
        prompt_suffix="Render like an ornate fantasy storybook illustration with glowing details, painterly textures, and magical atmosphere.",
        description="Fantasy storybook illustration",
    ),
    "pastel-dream": StyleProfile(
        id="pastel-dream",
        prompt_suffix="Render in a dreamy pastel palette with airy softness, diffused light, and delicate whimsical detail.",
        description="Pastel dreamscape",
    ),
    "gouache": StyleProfile(
        id="gouache",
        prompt_suffix="Render as a gouache illustration with velvety matte color, layered brushwork, and bold hand-painted shapes.",
        description="Gouache illustration",
    ),
    "charcoal": StyleProfile(
        id="charcoal",
        prompt_suffix="Render as a dramatic charcoal drawing with smudged shadows, textured paper, and expressive dark marks.",
        description="Charcoal drawing",
    ),
    "colored-pencil": StyleProfile(
        id="colored-pencil",
        prompt_suffix="Render as a colored pencil illustration with visible pencil texture, layered strokes, and vibrant hand-drawn shading.",
        description="Colored pencil illustration",
    ),
    "pastel-chalk": StyleProfile(
        id="pastel-chalk",
        prompt_suffix="Render as soft pastel chalk artwork with powdery blending, luminous color, and gentle textured edges.",
        description="Pastel chalk artwork",
    ),
    "fresco": StyleProfile(
        id="fresco",
        prompt_suffix="Render like an old fresco mural painted on plaster with weathered texture, muted pigments, and classical composition.",
        description="Fresco mural",
    ),
    "blueprint": StyleProfile(
        id="blueprint",
        prompt_suffix="Render as a technical blueprint drawing with crisp white linework, annotation-like detail, and a deep blue background.",
        description="Technical blueprint",
    ),
    "architectural-render": StyleProfile(
        id="architectural-render",
        prompt_suffix="Render like a polished architectural visualization with clean materials, realistic daylight, and design-magazine precision.",
        description="Architectural visualization",
    ),
    "vector-flat": StyleProfile(
        id="vector-flat",
        prompt_suffix="Render as clean modern vector art with simplified flat shapes, crisp edges, and bold graphic clarity.",
        description="Flat vector illustration",
    ),
    "linocut": StyleProfile(
        id="linocut",
        prompt_suffix="Render in a linocut print style with carved textures, bold high-contrast shapes, and hand-printed character.",
        description="Linocut print",
    ),
    "screenprint": StyleProfile(
        id="screenprint",
        prompt_suffix="Render as a screenprinted poster with layered inks, slightly imperfect registration, and striking graphic contrast.",
        description="Screenprinted poster",
    ),
    "airbrush": StyleProfile(
        id="airbrush",
        prompt_suffix="Render in a glossy retro airbrush style with smooth gradients, luminous highlights, and stylized 1980s flair.",
        description="Retro airbrush",
    ),
    "sci-fi-concept-art": StyleProfile(
        id="sci-fi-concept-art",
        prompt_suffix="Render as high-end sci-fi concept art with epic scale, atmospheric lighting, and polished entertainment-design detail.",
        description="Sci-fi concept art",
    ),
    "fantasy-concept-art": StyleProfile(
        id="fantasy-concept-art",
        prompt_suffix="Render as fantasy concept art with sweeping composition, rich detail, painterly lighting, and adventurous grandeur.",
        description="Fantasy concept art",
    ),
    "miniature-diorama": StyleProfile(
        id="miniature-diorama",
        prompt_suffix="Render as a handcrafted miniature diorama with tiny props, tactile materials, and charming scale-model realism.",
        description="Miniature diorama",
    ),
    "felt-plush": StyleProfile(
        id="felt-plush",
        prompt_suffix="Render as if made from felt and plush fabric with sewn seams, fuzzy texture, and cozy handmade charm.",
        description="Felt and plush craft",
    ),
    "balloon-sculpture": StyleProfile(
        id="balloon-sculpture",
        prompt_suffix="Render as a whimsical balloon sculpture with shiny twisted latex forms, playful proportions, and party-like color.",
        description="Balloon sculpture",
    ),
    "glass-art": StyleProfile(
        id="glass-art",
        prompt_suffix="Render as delicate glass art with translucent surfaces, internal reflections, and jewel-like color.",
        description="Glass art",
    ),
    "porcelain-figurine": StyleProfile(
        id="porcelain-figurine",
        prompt_suffix="Render as a glossy porcelain figurine with smooth glazed surfaces, delicate painted details, and collectible charm.",
        description="Porcelain figurine",
    ),
    "woodcarving": StyleProfile(
        id="woodcarving",
        prompt_suffix="Render as a hand-carved wooden sculpture with visible grain, chiseled texture, and artisan craftsmanship.",
        description="Wood carving",
    ),
    "embroidery": StyleProfile(
        id="embroidery",
        prompt_suffix="Render as embroidered textile art with stitched outlines, thread texture, and decorative fabric patterns.",
        description="Embroidery textile art",
    ),
    "lace-paper": StyleProfile(
        id="lace-paper",
        prompt_suffix="Render as intricate lace-like paper art with delicate cut patterns, airy negative space, and elegant handmade detail.",
        description="Lace paper art",
    ),
    "toy-box-3d": StyleProfile(
        id="toy-box-3d",
        prompt_suffix="Render as glossy stylized 3D toy-box art with chunky shapes, playful proportions, and collectible figurine appeal.",
        description="Stylized toy-like 3D",
    ),
    "magazine-editorial": StyleProfile(
        id="magazine-editorial",
        prompt_suffix="Render like a premium magazine editorial image with polished styling, sophisticated lighting, and fashion-shoot energy.",
        description="Magazine editorial",
    ),
    "surrealist": StyleProfile(
        id="surrealist",
        prompt_suffix="Render in a surrealist style with dream logic, uncanny juxtapositions, and poetic visual strangeness.",
        description="Surrealist art",
    ),
    "maximalist": StyleProfile(
        id="maximalist",
        prompt_suffix="Render in a maximalist style packed with ornate detail, layered patterns, bold color, and exuberant visual richness.",
        description="Maximalist design",
    ),
    "monochrome-ink": StyleProfile(
        id="monochrome-ink",
        prompt_suffix="Render in monochrome ink with elegant black linework, expressive shading, and crisp illustrative contrast.",
        description="Monochrome ink illustration",
    ),
}

ENVIRONMENT_VARIANTS: dict[str, EnvironmentVariant] = {
    "default": EnvironmentVariant(
        id="default",
        prompt_suffix="",
        description="No environment mutation",
    ),
    "underwater": EnvironmentVariant(
        id="underwater",
        prompt_suffix="Reimagine the scene as if it were underwater — floating hair, bubbles, rippling light (preserve all subjects).",
        description="Underwater theme",
    ),
    "space": EnvironmentVariant(
        id="space",
        prompt_suffix="Reimagine the scene set in outer space — stars, nebulae, zero-gravity floating (preserve all subjects).",
        description="Outer space theme",
    ),
    "enchanted-forest": EnvironmentVariant(
        id="enchanted-forest",
        prompt_suffix="Reimagine the scene in a magical enchanted forest with glowing mushrooms, fairy lights, and ancient trees (preserve all subjects).",
        description="Enchanted forest",
    ),
    "snowy-winter": EnvironmentVariant(
        id="snowy-winter",
        prompt_suffix="Reimagine the scene in a snowy winter wonderland with falling snowflakes, icicles, and frosted surfaces (preserve all subjects).",
        description="Snowy winter",
    ),
    "tropical-beach": EnvironmentVariant(
        id="tropical-beach",
        prompt_suffix="Reimagine the scene on a sunny tropical beach with palm trees, turquoise water, and white sand (preserve all subjects).",
        description="Tropical beach",
    ),
    "steampunk": EnvironmentVariant(
        id="steampunk",
        prompt_suffix="Reimagine the scene in a steampunk world with brass gears, steam pipes, Victorian machinery, and airships (preserve all subjects).",
        description="Steampunk world",
    ),
    "cyberpunk": EnvironmentVariant(
        id="cyberpunk",
        prompt_suffix="Reimagine the scene in a cyberpunk city with neon signs, rain-slicked streets, holograms, and towering skyscrapers (preserve all subjects).",
        description="Cyberpunk city",
    ),
    "medieval-castle": EnvironmentVariant(
        id="medieval-castle",
        prompt_suffix="Reimagine the scene inside a grand medieval castle with stone walls, torches, tapestries, and suits of armor (preserve all subjects).",
        description="Medieval castle",
    ),
    "candy-land": EnvironmentVariant(
        id="candy-land",
        prompt_suffix="Reimagine the scene in a candy land with lollipop trees, chocolate rivers, gumdrop hills, and cotton candy clouds (preserve all subjects).",
        description="Candy land",
    ),
    "autumn-harvest": EnvironmentVariant(
        id="autumn-harvest",
        prompt_suffix="Reimagine the scene in an autumn harvest setting with golden leaves, pumpkins, hay bales, and warm amber light (preserve all subjects).",
        description="Autumn harvest",
    ),
    "jungle": EnvironmentVariant(
        id="jungle",
        prompt_suffix="Reimagine the scene in a dense tropical jungle with vines, exotic flowers, parrots, and dappled sunlight through the canopy (preserve all subjects).",
        description="Tropical jungle",
    ),
    "wild-west": EnvironmentVariant(
        id="wild-west",
        prompt_suffix="Reimagine the scene in the Wild West with a dusty frontier town, wooden saloon, tumbleweed, and desert mesas (preserve all subjects).",
        description="Wild West frontier",
    ),
    "cloud-kingdom": EnvironmentVariant(
        id="cloud-kingdom",
        prompt_suffix="Reimagine the scene in a kingdom above the clouds with fluffy cloud platforms, golden staircases, and rainbow bridges (preserve all subjects).",
        description="Cloud kingdom",
    ),
    "prehistoric": EnvironmentVariant(
        id="prehistoric",
        prompt_suffix="Reimagine the scene in a prehistoric landscape with volcanoes, dinosaurs in the background, lush ferns, and a primeval sky (preserve all subjects).",
        description="Prehistoric world",
    ),
    "haunted-mansion": EnvironmentVariant(
        id="haunted-mansion",
        prompt_suffix="Reimagine the scene inside a spooky haunted mansion with cobwebs, creaky floors, floating candles, and friendly ghosts (preserve all subjects).",
        description="Haunted mansion",
    ),
    "garden-party": EnvironmentVariant(
        id="garden-party",
        prompt_suffix="Reimagine the scene as a lovely garden party with flowers, bunting, a tea set, and butterflies (preserve all subjects).",
        description="Garden party",
    ),
    "futuristic-city": EnvironmentVariant(
        id="futuristic-city",
        prompt_suffix="Reimagine the scene in a gleaming futuristic city with flying cars, glass towers, holographic billboards, and clean energy (preserve all subjects).",
        description="Futuristic city",
    ),
    "pirate-ship": EnvironmentVariant(
        id="pirate-ship",
        prompt_suffix="Reimagine the scene on the deck of a pirate ship sailing the high seas with billowing sails, treasure chests, and a Jolly Roger flag (preserve all subjects).",
        description="Pirate ship",
    ),
    "miniature-world": EnvironmentVariant(
        id="miniature-world",
        prompt_suffix="Reimagine the scene as if the subjects are tiny, living in a miniature world among everyday objects that tower over them (preserve all subjects).",
        description="Miniature/borrowers world",
    ),
    "aurora-borealis": EnvironmentVariant(
        id="aurora-borealis",
        prompt_suffix="Reimagine the scene under a spectacular aurora borealis with shimmering green and purple curtains of light in an Arctic landscape (preserve all subjects).",
        description="Northern lights",
    ),
    "desert-oasis": EnvironmentVariant(
        id="desert-oasis",
        prompt_suffix="Reimagine the scene at a desert oasis with golden dunes, date palms, shimmering water, and sunlit heat haze (preserve all subjects).",
        description="Desert oasis",
    ),
    "moon-base": EnvironmentVariant(
        id="moon-base",
        prompt_suffix="Reimagine the scene at a moon base with lunar dust, observation domes, scientific equipment, and Earth visible in the sky (preserve all subjects).",
        description="Moon base",
    ),
    "mars-colony": EnvironmentVariant(
        id="mars-colony",
        prompt_suffix="Reimagine the scene in a Mars colony with red rock terrain, habitat domes, rovers, and a dramatic dusty sky (preserve all subjects).",
        description="Mars colony",
    ),
    "volcanic-realm": EnvironmentVariant(
        id="volcanic-realm",
        prompt_suffix="Reimagine the scene in a volcanic realm with glowing lava flows, black rock, sparks in the air, and intense fiery light (preserve all subjects).",
        description="Volcanic realm",
    ),
    "crystal-cavern": EnvironmentVariant(
        id="crystal-cavern",
        prompt_suffix="Reimagine the scene inside a crystal cavern filled with towering gems, refracted light, and shimmering mineral walls (preserve all subjects).",
        description="Crystal cavern",
    ),
    "ice-palace": EnvironmentVariant(
        id="ice-palace",
        prompt_suffix="Reimagine the scene in a sparkling ice palace with frozen arches, crystalline chandeliers, and blue-white reflected light (preserve all subjects).",
        description="Ice palace",
    ),
    "fairy-village": EnvironmentVariant(
        id="fairy-village",
        prompt_suffix="Reimagine the scene in a fairy village with tiny lanterns, flower houses, winding paths, and magical woodland charm (preserve all subjects).",
        description="Fairy village",
    ),
    "dragon-lair": EnvironmentVariant(
        id="dragon-lair",
        prompt_suffix="Reimagine the scene inside a dragon's lair with treasure piles, glowing embers, ancient stone, and epic fantasy atmosphere (preserve all subjects).",
        description="Dragon lair",
    ),
    "royal-ballroom": EnvironmentVariant(
        id="royal-ballroom",
        prompt_suffix="Reimagine the scene in a royal ballroom with chandeliers, polished marble floors, gilded decor, and elegant grandeur (preserve all subjects).",
        description="Royal ballroom",
    ),
    "festival-midway": EnvironmentVariant(
        id="festival-midway",
        prompt_suffix="Reimagine the scene at a lively festival midway with string lights, colorful booths, ferris wheel glow, and celebratory energy (preserve all subjects).",
        description="Festival midway",
    ),
    "circus-big-top": EnvironmentVariant(
        id="circus-big-top",
        prompt_suffix="Reimagine the scene under a circus big top with striped canvas, spotlights, acrobatic flair, and vintage carnival excitement (preserve all subjects).",
        description="Circus big top",
    ),
    "arcade": EnvironmentVariant(
        id="arcade",
        prompt_suffix="Reimagine the scene in a retro arcade filled with glowing cabinets, blinking lights, patterned carpet, and playful game-room energy (preserve all subjects).",
        description="Retro arcade",
    ),
    "roller-rink": EnvironmentVariant(
        id="roller-rink",
        prompt_suffix="Reimagine the scene at a roller rink with disco lights, glossy floors, neon accents, and upbeat nostalgic fun (preserve all subjects).",
        description="Roller rink",
    ),
    "drive-in-movie": EnvironmentVariant(
        id="drive-in-movie",
        prompt_suffix="Reimagine the scene at a vintage drive-in movie theater with a giant screen, parked classic cars, twilight sky, and marquee lights (preserve all subjects).",
        description="Drive-in movie theater",
    ),
    "boardwalk": EnvironmentVariant(
        id="boardwalk",
        prompt_suffix="Reimagine the scene on a lively seaside boardwalk with carnival rides, snack stands, ocean breeze, and sun-faded charm (preserve all subjects).",
        description="Seaside boardwalk",
    ),
    "library-maze": EnvironmentVariant(
        id="library-maze",
        prompt_suffix="Reimagine the scene in an enormous library maze with towering bookshelves, rolling ladders, warm lamplight, and endless corridors of books (preserve all subjects).",
        description="Library maze",
    ),
    "observatory": EnvironmentVariant(
        id="observatory",
        prompt_suffix="Reimagine the scene in a grand observatory with brass telescopes, star charts, rotating domes, and celestial wonder (preserve all subjects).",
        description="Grand observatory",
    ),
    "train-station": EnvironmentVariant(
        id="train-station",
        prompt_suffix="Reimagine the scene in a bustling historic train station with arched glass ceilings, steam, luggage carts, and travel-adventure atmosphere (preserve all subjects).",
        description="Historic train station",
    ),
    "luxury-yacht": EnvironmentVariant(
        id="luxury-yacht",
        prompt_suffix="Reimagine the scene aboard a luxury yacht with polished decks, open sea views, bright sun, and upscale vacation atmosphere (preserve all subjects).",
        description="Luxury yacht",
    ),
    "mountaintop": EnvironmentVariant(
        id="mountaintop",
        prompt_suffix="Reimagine the scene on a dramatic mountaintop with sweeping views, crisp air, rocky peaks, and clouds below (preserve all subjects).",
        description="Mountaintop vista",
    ),
    "cherry-blossom": EnvironmentVariant(
        id="cherry-blossom",
        prompt_suffix="Reimagine the scene during cherry blossom season with drifting pink petals, soft spring light, and blooming trees all around (preserve all subjects).",
        description="Cherry blossom spring",
    ),
    "rainy-city": EnvironmentVariant(
        id="rainy-city",
        prompt_suffix="Reimagine the scene in a rainy city with reflective pavement, umbrellas, glowing windows, and moody atmospheric drizzle (preserve all subjects).",
        description="Rainy city streets",
    ),
    "sunflower-field": EnvironmentVariant(
        id="sunflower-field",
        prompt_suffix="Reimagine the scene in a vast sunflower field with golden blooms, warm summer light, and a cheerful countryside backdrop (preserve all subjects).",
        description="Sunflower field",
    ),
    "lavender-farm": EnvironmentVariant(
        id="lavender-farm",
        prompt_suffix="Reimagine the scene in a fragrant lavender farm with rows of purple blossoms, bees, and hazy golden-hour light (preserve all subjects).",
        description="Lavender farm",
    ),
    "safari": EnvironmentVariant(
        id="safari",
        prompt_suffix="Reimagine the scene on an African safari with tall grasses, acacia trees, warm sunset tones, and distant wildlife silhouettes (preserve all subjects).",
        description="Safari landscape",
    ),
    "zen-garden": EnvironmentVariant(
        id="zen-garden",
        prompt_suffix="Reimagine the scene in a tranquil zen garden with raked sand, stone lanterns, bonsai forms, and serene meditative balance (preserve all subjects).",
        description="Zen garden",
    ),
    "floating-islands": EnvironmentVariant(
        id="floating-islands",
        prompt_suffix="Reimagine the scene among floating islands in the sky with waterfalls spilling into clouds, hanging bridges, and fantastical open air (preserve all subjects).",
        description="Floating islands",
    ),
    "submarine-lab": EnvironmentVariant(
        id="submarine-lab",
        prompt_suffix="Reimagine the scene inside an underwater submarine laboratory with panoramic ocean windows, instrument panels, and marine life outside (preserve all subjects).",
        description="Submarine laboratory",
    ),
    "robot-factory": EnvironmentVariant(
        id="robot-factory",
        prompt_suffix="Reimagine the scene in a robot factory with assembly lines, articulated machinery, sparks, and futuristic industrial design (preserve all subjects).",
        description="Robot factory",
    ),
    "treetop-canopy": EnvironmentVariant(
        id="treetop-canopy",
        prompt_suffix="Reimagine the scene high in a treetop canopy with rope bridges, elevated platforms, filtered sunlight, and lush green surroundings (preserve all subjects).",
        description="Treetop canopy",
    ),
    "ancient-ruins": EnvironmentVariant(
        id="ancient-ruins",
        prompt_suffix="Reimagine the scene among ancient ruins with weathered stone, creeping vines, carved columns, and timeless mystery (preserve all subjects).",
        description="Ancient ruins",
    ),
    "secret-laboratory": EnvironmentVariant(
        id="secret-laboratory",
        prompt_suffix="Reimagine the scene in a secret laboratory with glowing experiment tubes, advanced gadgets, control panels, and mad-science intrigue (preserve all subjects).",
        description="Secret laboratory",
    ),
}


def get_style_profile(profile_id: Optional[str]) -> Optional[StyleProfile]:
    """Resolve style profile by ID. Returns None for unknown/empty."""
    if not profile_id or not str(profile_id).strip():
        return None
    return STYLE_PROFILES.get(str(profile_id).strip().lower())


def get_environment_variant(variant_id: Optional[str]) -> Optional[EnvironmentVariant]:
    """Resolve environment variant by ID. Returns None for unknown/empty."""
    if not variant_id or not str(variant_id).strip():
        return None
    return ENVIRONMENT_VARIANTS.get(str(variant_id).strip().lower())


def random_style_profile() -> StyleProfile:
    """Return a randomly selected style profile (may be 'default' = no overlay)."""
    return random.choice(list(STYLE_PROFILES.values()))


def random_environment_variant() -> EnvironmentVariant:
    """Return a randomly selected environment variant (may be 'default' = no mutation)."""
    return random.choice(list(ENVIRONMENT_VARIANTS.values()))
