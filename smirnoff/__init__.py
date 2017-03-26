try:
    import openeye
    # These can only be imported if openeye tools are available
    from smirnoff.forcefield import *
    from smirnoff.forcefield_utils import *
    from smirnoff.utils import *

except Exception as e:
    print(e)
    print('Warning: Cannot import openeye toolkit; not all functionality will be available.')
