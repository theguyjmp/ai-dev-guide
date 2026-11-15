# A good acceptance test (overhaul-plan Steps 3 and 5)

## Example (a CLI app; F02 is `shop product add`, F03 is `shop cart show`)
`| F04 | 1 | cart | F02,F03 | cli: shop cart add <sku> <qty> | tests/acceptance/test_cart.py::test_f04_cart_add | planned | src/shop/cart/add.py | F02:behaviour, F03:stub |`
```python
# tests/acceptance/_entry.py: the only door from a test into the product
import os, subprocess, sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "src"
def run_cli(data, *args):
    env = {**os.environ, "PYTHONPATH": str(SRC), "SHOP_DATA": str(data)}
    return subprocess.run([sys.executable, "-m", "shop", *args], cwd=data, env=env,
                          capture_output=True, text=True, timeout=60, check=False)
# tests/acceptance/test_cart.py
from _entry import run_cli
def test_f04_cart_add(tmp_path):
    assert run_cli(tmp_path, "product", "add", "MUG1", "Mug", "4.50").returncode == 0
    added = run_cli(tmp_path, "cart", "add", "MUG1", "2")
    assert added.returncode == 0, added.stderr
    assert "MUG1" in added.stdout and "9.00" in added.stdout  # F03 (totals) is a code dep: its stub is enough
    bad = run_cli(tmp_path, "cart", "add", "NOPE", "1")
    assert bad.returncode != 0 and "unknown sku" in bad.stderr.lower()
```
Good: only public commands of F04 and its deps, its own data dir, what the user sees plus an error case, red today,
`-k test_f04_cart_add` runs exactly it. Bad: importing `shop.cart`, a mocked store, or checking only the exit code.
