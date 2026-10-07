import sys
sys.path.insert(0, '.')
from fizz import label
assert label(14) == 'FizzBuzz'
assert label(2) == 'Fizz'
assert label(7) == 'Buzz'
assert label(1) == '1'
print('ok')
