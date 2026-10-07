import sys
sys.path.insert(0, '.')
from fizz import label
assert label(45) == 'FizzBuzz'
assert label(5) == 'Fizz'
assert label(9) == 'Buzz'
assert label(1) == '1'
print('ok')
