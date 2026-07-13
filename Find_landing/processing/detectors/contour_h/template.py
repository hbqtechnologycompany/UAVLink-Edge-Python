import json
import os
from typing import Any, Tuple


def load_template(find_landing_dir: str) -> Tuple[Any, Any]:
    import find

    landing_config_path = os.path.join(find_landing_dir, "landing_config.json")
    template_name = "H"
    if os.path.exists(landing_config_path):
        try:
            with open(landing_config_path, "r") as f:
                landing_config = json.load(f)
                template_name = landing_config.get("template", "H")
        except Exception:
            pass

    template_path = os.path.join(find_landing_dir, "templates", f"{template_name}.png")
    if not os.path.exists(template_path):
        template_path = os.path.join(find_landing_dir, "templates", "H.png")
    return find.load_template(template_path)
