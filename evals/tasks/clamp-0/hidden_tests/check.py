import sys
sys.path.insert(0, '.')
from bounds import clamp
assert clamp(-7, 0, 10) == 0
assert clamp(17, 0, 10) == 10
assert clamp(5, 0, 10) == 5
print('ok')
