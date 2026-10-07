import sys
sys.path.insert(0, '.')
from bounds import clamp
assert clamp(3, 10, 20) == 10
assert clamp(27, 10, 20) == 20
assert clamp(15, 10, 20) == 15
print('ok')
