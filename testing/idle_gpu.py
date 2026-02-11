import torch
import time

x = torch.tensor([1.0], device="cuda")

while True:
    x = x + 1
    torch.cuda.synchronize()
    time.sleep(30)

