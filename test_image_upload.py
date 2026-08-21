"""
Quick test: upload a synthetic microscopy image for a given result_id.

Usage:
    python test_image_upload.py

By default targets result: fee74d47-10c8-4654-864e-4f419f3c6589 (Ana Reyes)
Edit RESULT_ID below to target a different result.
"""
import asyncio
import io
import random
import sys

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter

load_dotenv()

BASE_URL = "http://localhost:8000/api/v1"
RESULT_ID = "fee74d47-10c8-4654-864e-4f419f3c6589"
MEDTECH_USERNAME = "medtech"
MEDTECH_PASSWORD = "password123"


def _make_synthetic_microscopy_image() -> bytes:
    """Generate a 640x480 grayscale image that loosely resembles a urine microscopy field."""
    random.seed(42)
    img = Image.new("RGB", (640, 480), color=(20, 20, 30))
    draw = ImageDraw.Draw(img)

    # Background noise
    for _ in range(4000):
        x, y = random.randint(0, 639), random.randint(0, 479)
        r = random.randint(1, 3)
        gray = random.randint(30, 70)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(gray, gray, gray + 10))

    # Simulate cells (circular blobs)
    for _ in range(20):
        x, y = random.randint(40, 600), random.randint(40, 440)
        r = random.randint(12, 22)
        c = random.randint(140, 200)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(c, c - 10, c - 30), outline=(c + 30, c + 20, c))

    # Simulate smaller particles
    for _ in range(50):
        x, y = random.randint(10, 630), random.randint(10, 470)
        r = random.randint(3, 7)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(100, 120, 150))

    img = img.filter(ImageFilter.GaussianBlur(radius=1))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def main():
    from src.urolens.core.supabase import supabase

    # 1. Get specimen_id from result_id
    print(f"Looking up specimen for result_id={RESULT_ID} ...")
    res = await supabase.table("analysis_results").select("result_id, specimen_id").eq("result_id", RESULT_ID).limit(1).execute()
    if not res.data:
        print(f"ERROR: result_id {RESULT_ID!r} not found in analysis_results.")
        sys.exit(1)

    specimen_id = res.data[0]["specimen_id"]
    print(f"Found specimen_id: {specimen_id}")

    # 2. Login as medtech to get JWT
    print(f"\nLogging in as {MEDTECH_USERNAME!r} ...")
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        login_resp = await client.post("/auth/login", json={"username": MEDTECH_USERNAME, "password": MEDTECH_PASSWORD})
        if login_resp.status_code != 200:
            print(f"ERROR: Login failed {login_resp.status_code}: {login_resp.text}")
            sys.exit(1)

        token = login_resp.json().get("access_token")
        if not token:
            print(f"ERROR: No access_token in response: {login_resp.json()}")
            sys.exit(1)

        print(f"Got JWT (first 40 chars): {token[:40]}...")

        # 3. Generate synthetic image
        print("\nGenerating synthetic 640x480 microscopy image ...")
        image_bytes = _make_synthetic_microscopy_image()
        print(f"Image size: {len(image_bytes):,} bytes")

        # 4. Upload
        print(f"\nUploading to POST {BASE_URL}/images/upload ...")
        upload_resp = await client.post(
            "/images/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("microscopy_test.jpg", image_bytes, "image/jpeg")},
            data={"specimen_id": specimen_id},
        )

        print(f"Response status: {upload_resp.status_code}")
        if upload_resp.status_code in (200, 201):
            data = upload_resp.json()
            print(f"SUCCESS!")
            print(f"  result_id:  {data.get('result_id')}")
            print(f"  image_id:   {data.get('image_id')}")
            print(f"  status:     {data.get('status')}")
            print(f"  ai_findings:{data.get('ai_findings')}")
            print(f"\nRefresh the supervisor result review page — the image should now appear.")
        else:
            print(f"ERROR: {upload_resp.text}")


if __name__ == "__main__":
    asyncio.run(main())
