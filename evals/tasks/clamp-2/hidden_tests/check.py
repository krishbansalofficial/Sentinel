import sys
sys.path.insert(0, '.')
from bounds import clamp
assert clamp(-6, 1, 3) == 1
assert clamp(10, 1, 3) == 3
assert clamp(2, 1, 3) == 2
print('ok')
