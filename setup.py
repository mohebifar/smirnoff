import os
from os.path import relpath, join
from setuptools import setup

def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()

def find_package_data(data_root, package_root):
    files = []
    for root, dirnames, filenames in os.walk(data_root):
        for fn in filenames:
            files.append(relpath(join(root, fn), package_root))
    return files

setup(
    name = "smirnoff",
    version = "0.2.0",
    author = "The Open ForceField Group",
    author_email = "john.chodera@choderalab.org",
    description = ("Automated Bayesian atomtype sampling"),
    license = "MIT",
    keywords = "Bayesian atomtype sampling forcefield parameterization",
    url = "http://github.com/open-forcefield-group/smirnoff",
    packages=['smirnoff', 'smirnoff/tests', 'smirnoff/data'],
    long_description=read('README.md'),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Utilities",
        "License :: OSI Approved :: MIT",
    ],
    entry_points={'console_scripts': ['smirnoff = smirnoff.cli_smirnoff:main', 'smirky = smirnoff.cli_smirky:main']},
    package_data={'smirnoff': find_package_data('smirnoff/data', 'smirnoff')},
)
