import sys
sys.path.insert(0, '.')
from bounds import clamp
assert clamp(-107, -100, 0) == -100
assert clamp(7, -100, 0) == 0
assert clamp(-50, -100, 0) == -50
print('ok')
