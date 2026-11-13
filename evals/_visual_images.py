import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VISUAL_IMAGE_NAMES = ("screen.png", "occluded-screen.png", "schematic-camera.png")


def generate_visual_images(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=30)
    screen = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(screen)
    draw.text((40, 40), "Project Orchid", font=font, fill="black")
    draw.text((40, 130), "Beta: 17 March 2027", font=font, fill="black")
    draw.text((40, 230), "Owner: Budi", font=font, fill="black")
    screen.save(root / "screen.png")
    draw.rectangle((120, 120, 450, 185), fill=(110, 110, 110))
    screen.save(root / "occluded-screen.png")
    camera = Image.new("RGB", (640, 360), (230, 230, 230))
    draw = ImageDraw.Draw(camera)
    draw.ellipse((260, 20, 380, 140), fill=(170, 130, 95))
    draw.rectangle((200, 145, 440, 359), fill=(0, 60, 230))
    draw.rectangle((470, 230, 540, 330), fill=(230, 0, 0))
    draw.ellipse((520, 245, 570, 310), outline=(230, 0, 0), width=10)
    camera.save(root / "schematic-camera.png")


def image_pixel_digest(path: Path) -> str:
    with Image.open(path) as image:
        return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


if __name__ == "__main__":
    generate_visual_images(Path(__file__).parent / "fixtures" / "visual-inspection")
