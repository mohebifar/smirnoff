#!/bin/env python

import lxml.etree as etree
import numpy as np
from smarty import ForceField

# Function definitions for parsing sections within parameter file
def _parse_nonbon_line( line ):
    """Parse an AMBER frcmod nonbon line and return relevant parameters in a dictionary. AMBER uses rmin_half and epsilon in angstroms and kilocalories per mole."""
    tmp = line.split()
    params = {}
    params['smirks'] = tmp[0]
    params['rmin_half'] = tmp[1]
    params['epsilon'] = tmp[2]

    return params

def _parse_bond_line( line ):
    """Parse an AMBER frcmod BOND line and return relevant parameters in a dictionary. AMBER uses length and force constant, with the factor of two dropped. Here we multiply by the factor of two before returning. Units are angstroms and kilocalories per mole per square angstrom."""

    tmp = line.split()
    params = {}
    params['smirks'] = tmp[0]
    params['k'] = str(2*float(tmp[1]))
    params['length'] = tmp[2]
    return params

def _parse_angl_line( line ):
    """Parse an AMBER frcmod ANGL line and return relevant parameters in a dictionary. AMBER uses angle and force constant, with the factor of two dropped. Here we multiply by the factor of two before returning. Units are degrees and kilocalories per mole."""

    tmp = line.split()
    params = {}
    params['smirks'] = tmp[0]
    params['k'] = str(2*float(tmp[1]))
    params['angle'] = tmp[2]
    return params

def _parse_dihe_line( line ):
    """Parse an AMBER frcmod DIHE line and return relevant parameters in a dictionary. Units for k are kilocalories per mole."""

    tmp = line.split()
    params = {}
    params['smirks'] = tmp[0]
    params['idivf1'] = tmp[1]
    params['k1'] = tmp[2]
    params['phase1'] = tmp[3]
    params['periodicity1'] = str(int(np.abs(float(tmp[4]))))
    return params

def _parse_impr_line( line ):
    """Parse an AMBER frcmod DIHE line and return relevant parameters in a dictionary. Units for k are kilocalories per mole."""

    tmp = line.split()
    params = {}
    params['smirks'] = tmp[0]
    params['k1'] = tmp[1]
    params['phase1'] = tmp[2]
    params['periodicity1'] = str(int(np.abs(float(tmp[3]))))
    return params

# Main conversion functionality
def convert_frcmod_to_ffxml( infile, inxml, outxml ):
    """Convert a modified AMBER frcmod (with SMIRKS replacing atom types) to SMIRFF ffxml format by inserting parameters into a template ffxml file.

    Parameters
    ----------
    infile : str
        File name of input SMIRKS-ified frcmod file containing parameters
    inxml : str
        File name of template SMIRFF FFXML file into which to insert these parameters.
    outxml : str
        File name of resulting output SMIRFF FFXML

    Notes:
    -------
    Input XML file will normally be the template of a SMIRFF XML file without any parameters present (but with requisite force types already specified).
    """


    # Obtain sections from target file
    file = open(infile, 'r')
    text = file.readlines()
    file.close()
    sections = {}
    # Section names from frcmod which we will parse
    secnames = ['NONBON', 'BOND', 'ANGL', 'IMPR', 'DIHE']
    # Tags that will be used in the FFXML for these (same order)
    tag = ['Atom', 'Bond', 'Angle', 'Improper', 'Proper']
    # Force names in the FFXML (same order)
    force_section = ['NonbondedForce', 'HarmonicBondForce', 'HarmonicAngleForce', 'PeriodicTorsionForce', 'PeriodicTorsionForce']
    ct = 0
    thissec = None
    while ct < len(text):
        line = text[ct]
        tmp = line.split()

        # Skip lines starting with comment or which are blank
        if line[0]=='#' or len(tmp) < 1:
            ct+=1
            continue

        # Check first entry to see if it's a section name, if so initialize storage
        if tmp[0] in secnames:
            thissec = tmp[0]
            sections[thissec] = []
        # Otherwise store
        else:
            sections[thissec].append(line)

        ct+=1


    # Read template forcefield file
    ff = ForceField(inxml)
    # Use functions to parse sections from target file and add parameters to force field
    param_id_by_section={}
    param_prefix_by_sec = {'NONBON':'n' , 'BOND':'b', 'ANGL':'a', 'DIHE':'t', 'IMPR':'i'}
    for (idx, name) in enumerate(secnames):
        param_id_by_section[name] = 1
        for line in sections[name]:
            # Parse line for parameters
            if name=='NONBON':
                params = _parse_nonbon_line(line)
            elif name=='BOND':
                params = _parse_bond_line(line)
            elif name=='DIHE':
                params = _parse_dihe_line(line)
            elif name=='IMPR':
                params = _parse_impr_line(line)
            elif name=='ANGL':
                params = _parse_angl_line(line)

            # Add parameter ID
            params['id'] = param_prefix_by_sec[name]+str( param_id_by_section[name] )

            smirks = params['smirks']

            # If it's not a torsion, just store in straightforward way
            if not (name=='IMPR' or name=='DIHE'):
                # Check for duplicates first
                if ff.getParameter( smirks, force_type = force_section[idx] ):
                    raise ValueError("Error: parameter for %s is already present in forcefield." % smirks )
                else:
                    ff.addParameter( params, smirks, force_section[idx], tag[idx] )

                # Increment parameter id
                param_id_by_section[name] +=1
            # If it's a torsion, check to see if there are already parameters and
            # if so, add a new term to this torsion
            else:
                # If we have parameters already
                oldparams = ff.getParameter(smirks, force_type=force_section[idx])
                if oldparams:
                    # Find what number to use
                    idnr = 1
                    paramtag = 'k%s' % idnr
                    while paramtag in params:
                        idnr+=1
                        paramtag = 'k%s' % idnr
                    # Construct new param object with updated numbers
                    for paramtag in ('periodicity1', 'phase1', 'idivf1', 'k1'):
                        if paramtag in params:
                            val = params.pop(paramtag)
                            oldparams[paramtag[:-1]+str(idnr) ] = val
                    # Store
                    ff.setParameter( oldparams, smirks=smirks, force_type=force_section[idx])
                else:
                    # Otherwise, just store new parameters
                    ff.addParameter( params, smirks, force_section[idx], tag[idx])
                    # Increment parameter ID
                    param_id_by_section[name] += 1


    # Write SMIRFF XML file
    ff.writeFile(outxml)

    # Roundtrip to fix formatting (for some reason etree won't format it properly on first write after modification)
    tmp = ForceField(outxml)
    tmp.writeFile(outxml)


if __name__=="__main__":
    from optparse import OptionParser
    usage_string="""\
    Convert specified SMIRKS-ified AMBER frcmod file into SMIRFF FFXML format, inserting converted parameters into a template FFXML file and writing to a new output file.

    usage: convert_frcmod.py --frcmod test.frcmod --template template.ffxml --xml test.ffxml
    """
    parser = OptionParser(usage=usage_string)

    parser.add_option('-f', '--frcmod', type = "string", dest='infile', default = None, action="store", help="Name of input smirks-ified frcmod file.")
    parser.add_option('-t', '--template', type="string", dest='inxml', default = None, action ="store", help="Name of template SMIRFF ffxml file.")
    parser.add_option('-o', '--xml', type="string", dest='outxml', default =None, action="store", help="Name of output SMIRFF ffxml file.")
    (options,args) = parser.parse_args()

    if (options.infile is None) or (options.inxml is None) or (options.outxml is None):
        parser.print_help()
        parser.error("Input frcmod and template files and output FFXML file must be specified.")


    convert_frcmod_to_ffxml( options.infile, options.inxml, options.outxml )
