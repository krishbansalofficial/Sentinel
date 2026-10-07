import sys
sys.path.insert(0, '.')
from bounds import clamp
assert clamp(-12, -5, 5) == -5
assert clamp(12, -5, 5) == 5
assert clamp(0, -5, 5) == 0
print('ok')
