import sys
sys.path.insert(0, '.')
from series import sum_to
assert sum_to(12) == 78
assert sum_to(1) == 1 and sum_to(0) == 0
print('ok')
