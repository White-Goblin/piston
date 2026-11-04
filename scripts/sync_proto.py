"""Copy piston_device/proto/ into the integration (HACS ships only custom_components)."""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "piston_device", "proto")
DST = os.path.join(ROOT, "custom_components", "piston", "proto")


def sync() -> None:
    os.makedirs(DST, exist_ok=True)
    for name in os.listdir(DST):
        if name.endswith(".py") and not os.path.exists(os.path.join(SRC, name)):
            os.remove(os.path.join(DST, name))
    for name in os.listdir(SRC):
        if name.endswith(".py"):
            shutil.copyfile(os.path.join(SRC, name), os.path.join(DST, name))


if __name__ == "__main__":
    sync()
