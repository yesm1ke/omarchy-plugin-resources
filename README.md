# Resources for the Omarchy bar

[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi&logoColor=white)](https://ko-fi.com/mike_g)

Four tiny meters in the Omarchy bar — CPU, RAM, GPU and disk — that show at a
glance how busy the machine is, in about the room of one icon.

![The meters next to the clock and the weather in the Omarchy bar](docs/bar.png)

## What you see

![CPU load rising to 100 %, the CPU meter turning red after a few seconds, then everything settling again](docs/meters.gif)

From left to right: CPU, RAM, GPU, disk. Each meter is a small track that
fills from the bottom with the current usage.
A meter turns to the theme's urgent colour (red above) when it crosses its
threshold:

| Meter | Shows | Turns urgent at |
|---|---|---|
| CPU | load over all cores since the previous poll | 80 %, held for three polls in a row (6 s), so short spikes don't flash |
| RAM | memory in use | 50 % |
| GPU | GPU utilisation (NVIDIA only) | 50 % |
| Disk | space used on the root filesystem `/` | 70 % |

The animation is a real recording at normal speed: a few seconds of full
CPU load. The CPU meter fills at once and turns red after the third poll; the
GPU meter flashes red while the compositor works harder.

Hover the meters for the exact numbers:

![Tooltip: CPU 7 % · RAM 24 % · GPU 28 % · Disk 26 %](docs/tooltip.png)

Click them to open Activity (`btop`, the same as <kbd>Super</kbd> + <kbd>Ctrl</kbd> + <kbd>T</kbd>).

The meters use the bar's own foreground and urgent colours, so they match any
Omarchy theme.

## Install

```sh
omarchy plugin add https://github.com/yesm1ke/omarchy-plugin-resources --enable
```

`--enable` asks which bar section to put it in. To move it later, for example
right after the clock:

```sh
omarchy bar move io.github.yesm1ke.resources --after omarchy.clock
```

Update with `omarchy plugin update`.

## How it works

Every two seconds one small shell command reads:

- CPU: the first line of `/proc/stat`. Usage is the change between two
  samples, so the CPU meter reads 0 % for the first two seconds after the shell
  starts.
- RAM: `free -b` (used of total).
- GPU: `nvidia-smi --query-gpu=utilization.gpu`. Without an NVIDIA GPU (or
  with its driver down) it prints nothing and the GPU meter is not shown.
- Disk: `df --output=pcent /`.

Each value is tagged in the output, so a missing `nvidia-smi` cannot shift the
others. Nothing is written anywhere and nothing runs between polls. The
thresholds are constants at the top of `Resources.qml`.

Made for a horizontal bar (top or bottom).

## License

MIT, see [LICENSE](LICENSE).
