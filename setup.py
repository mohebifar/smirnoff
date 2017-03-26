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
    name = "smarty",
    version = "0.1.6",
    author = "John Chodera, David Mobley, and others",
    author_email = "john.chodera@choderalab.org",
    description = ("Automated Bayesian atomtype sampling"),
    license = "MIT",
    keywords = "Bayesian atomtype sampling forcefield parameterization",
    url = "http://github.com/open-forcefield-group/smarty",
    packages=['smarty', 'smarty/tests', 'smarty/data'],
    long_description=read('README.md'),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Topic :: Utilities",
        "License :: OSI Approved :: MIT",
    ],
    entry_points={'console_scripts': ['smarty = smarty.cli_smarty:main', 'smirky = smarty.cli_smirky:main']},
    package_data={'smarty': find_package_data('smarty/data', 'smarty')},
)
