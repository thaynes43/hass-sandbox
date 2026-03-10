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
