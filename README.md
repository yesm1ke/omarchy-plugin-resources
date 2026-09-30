# Resources for the Omarchy bar

Compact CPU, RAM, GPU and root-filesystem disk meters in the Omarchy bar, polled every two
seconds. Exact values are in the hover tooltip; a click opens Activity
(`btop`). A meter turns to the urgent colour at a threshold (CPU 80 % held for
three polls, RAM 50 %, GPU 50 %, disk 70 %).

## Install

```sh
omarchy plugin add https://github.com/yesm1ke/omarchy-plugin-resources --enable
```

The GPU meter appears only on NVIDIA GPUs (it reads `nvidia-smi`); the others
work everywhere.

## License

MIT, see [LICENSE](LICENSE).
