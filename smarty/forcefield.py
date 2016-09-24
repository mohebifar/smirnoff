#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================

"""
forcefield.py

OpenMM ForceField replacement using SMIRKS-based matching.

AUTHORS

John D. Chodera <john.chodera@choderalab.org>
David L. Mobley <dmobley@mobleylab.org>

Baseed on simtk.openmm.app.forcefield written by Peter Eastman.

TODO:
* Constraint handling
* Move utility functions like 'generateTopologyFromOEMol()' elsewhere?
* Use xml parser with 'sourceline' node attributes to aid debugging
http://stackoverflow.com/questions/6949395/is-there-a-way-to-get-a-line-number-from-an-elementtree-element
"""
#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import sys
import string

import lxml.etree as etree

from simtk.openmm.app import element as elem
from simtk.openmm.app import Topology

import os
import math
import copy
import re
import numpy
import random

import openeye.oechem
import openeye.oeomega
import openeye.oequacpac

from openeye import oechem, oequacpac

from simtk import openmm, unit

import time

import networkx

import smarty.environment as env

#=============================================================================================
# PRIVATE SUBROUTINES
#=============================================================================================

def getSMIRKSMatches_OEMol(oemol, smirks, aromaticity_model = None):
    """Find all sets of atoms in the provided oemol that match the provided SMIRKS strings.

    Parameters
    ----------
    oemol : OpenEye oemol
        oemol to process with the SMIRKS in order to find matches
    smirks : str
        SMIRKS string with tagged atoms.
        If there are N tagged atoms numbered 1..N, the resulting matches will be N-tuples of atoms that match the corresponding tagged atoms.
    aromaticity_model : str (optional)
        OpenEye aromaticity model designation as a string, such as "OEAroModel_MDL". Default: None. If none is provided, molecule is processed exactly as provided; otherwise it is prepared with this aromaticity model prior to querying.

    Returns
    -------
    matches : list of tuples of atoms numbers
        matches[index] is an N-tuple of atom numbers from the oemol
        Matches are returned in no guaranteed order.
    """

    # Make a copy of molecule so we don't influence original (probably safer than deepcopy per C Bayly)
    mol = oechem.OEMol(oemol)

    # Set up query.
    qmol = oechem.OEQMol()
    if not oechem.OEParseSmarts(qmol, smirks):
        raise Exception("Error parsing SMIRKS '%s'" % smirks)

    # Determine aromaticity model
    if aromaticity_model:
        if type(aromaticity_model) == str:
            # Check if the user has provided a manually-specified aromaticity_model
            if hasattr(oechem, aromaticity_model):
                oearomodel = getattr(oechem, aromaticity_model)
            else:
                raise ValueError("Error: provided aromaticity model not recognized by oechem.")
        else:
            raise ValueError("Error: provided aromaticity model must be a string.")

        # If aromaticity model was provided, prepare molecule
        oechem.OEClearAromaticFlags( mol)
        oechem.OEAssignAromaticFlags( mol, oearomodel)
        # avoid running OEPrepareSearch or we lose desired aromaticity, so instead:
        oechem.OEAssignHybridization( mol)
        oechem.OEAssignFormalCharges( mol)
        oechem.OEAssignImplicitHydrogens( mol)

    # Perform matching on each mol
    matches = list()

    # We require non-unique matches, i.e. all matches
    unique = False
    ss = oechem.OESubSearch(qmol)
    matches = []
    for match in ss.Match( mol, unique):
        # Compile list of atom indices that match the pattern tags
        atom_indices = dict()
        for ma in match.GetAtoms():
            if ma.pattern.GetMapIdx() != 0:
                atom_indices[ma.pattern.GetMapIdx()-1] = ma.target.GetIdx()
        # Compress into list
        atom_indices = [ atom_indices[index] for index in range(len(atom_indices)) ]
        # Store
        matches.append( tuple(atom_indices) )

    return matches

#=============================================================================================
# Augmented Topology
#=============================================================================================

def generateTopologyFromOEMol(molecule):
    """
    Generate an OpenMM Topology object from an OEMol molecule.

    Parameters
    ----------
    molecule : openeye.oechem.OEMol
        The molecule from which a Topology object is to be generated.

    Returns
    -------
    topology : simtk.openmm.app.Topology
        The Topology object generated from `molecule`.

    """
    # Create a Topology object with one Chain and one Residue.
    from simtk.openmm.app import Topology
    topology = Topology()
    chain = topology.addChain()
    resname = molecule.GetTitle()
    residue = topology.addResidue(resname, chain)

    # Create atoms in the residue.
    for atom in molecule.GetAtoms():
        name = atom.GetName()
        element = elem.Element.getByAtomicNumber(atom.GetAtomicNum())
        atom = topology.addAtom(name, element, residue)

    # Create bonds.
    atoms = { atom.name : atom for atom in topology.atoms() }
    for bond in molecule.GetBonds():
        topology.addBond(atoms[bond.GetBgn().GetName()], atoms[bond.GetEnd().GetName()])

    return topology

def generateGraphFromTopology(topology):
    """Geneate a NetworkX graph from a Topology object.

    Parameters
    ----------
    topology : simtk.openmm.app.Topology
        The source topology.

    Returns
    -------
    graph : networkx.Graph
        The resulting graph, with nodes labeled with atom indices and elements

    """
    import networkx as nx
    # Create graph of atoms connected by bonds.
    G = nx.Graph()
    for atom in topology.atoms():
        G.add_node(atom.index, element=atom.element)
    for (atom1, atom2) in topology.bonds():
        G.add_edge(atom1.index, atom2.index)

    return G

class _Topology(Topology):
    """Augmented Topology object which adds:

    self._reference_molecules is a list of OEMol for the reference molecules
    self._reference_to_topology_atom_mappings[reference_molecule] is a list of atom indices mapping a reference molecule atom index to the topology atom index
    self._bondorders is a list of floating point bond orders for the bonds in the Topology.
    self._bondorders_by_atomindices is a dict of floating point bond orders for the bonds in the Topology, keyed by indices of the atoms involved.
    """
    def __init__(self, topology, reference_molecules):
        """
        Parameters
        ----------
        topology : simtk.openmm.app.Topology
            The Topology object to initialize this one from.
        reference_molecules : list of openeye.oechem.OEMol
            The list of reference molecules in the Topology.

        """
        # Initialize.
        super(_Topology, self).__init__()

        # TODO: Find a way to avoid having this be fragile based on internal representation of Topology.
        # TODO: Should this also use a deepcopy of 'topology' first?
        self._chains = topology._chains
        self._numResidues = topology._numResidues
        self._numAtoms = topology._numAtoms
        self._bonds = topology._bonds
        self._periodicBoxVectors = topology._periodicBoxVectors

        # Store reference molecules.
        # TODO: Deep copy?
        self._reference_molecules = reference_molecules

        # Identify all molecules and atom mappings.
        self._identifyMolecules()

        # Get/initialize bond orders
        self._updateBondOrders()

    def _identifyMolecules(self):
        """Identify all unique reference molecules and atom mappings to all instances in the Topology.
        """
        import networkx as nx
        from networkx.algorithms import isomorphism

        # Generate list of topology atoms.
        atoms = [ atom for atom in self.atoms() ]

        # Generate graphs for reference molecules.
        self._reference_molecule_graphs = list()
        for reference_molecule in self._reference_molecules:
            # Generate Topology
            reference_molecule_topology = generateTopologyFromOEMol(reference_molecule)
            # Generate Graph
            reference_molecule_graph = generateGraphFromTopology(reference_molecule_topology)
            self._reference_molecule_graphs.append(reference_molecule_graph)

        # Generate a graph for the current topology.
        G = generateGraphFromTopology(self)

        # Extract molecules (as connected component subgraphs).
        self._reference_to_topology_atom_mappings = { reference_molecule : list() for reference_molecule in self._reference_molecules }
        for molecule_graph in nx.connected_component_subgraphs(G):
            # Check if we have already stored a reference molecule for this molecule.
            reference_molecule_exists = False
            for (reference_molecule_graph, reference_molecule) in zip(self._reference_molecule_graphs, self._reference_molecules):
                GM = isomorphism.GraphMatcher(molecule_graph, reference_molecule_graph)
                if GM.is_isomorphic():
                    # This molecule is present in the list of unique reference molecules.
                    reference_molecule_exists = True
                    # Add the reference atom mappings.
                    reference_to_topology_atom_mapping = dict()
                    for (topology_atom, reference_atom) in GM.mapping.items():
                        reference_to_topology_atom_mapping[reference_atom] = topology_atom
                    self._reference_to_topology_atom_mappings[reference_molecule].append(reference_to_topology_atom_mapping)
                    # Break out of the search loop.
                    break

            # If the reference molecule could not be found, throw an exception.
            if not reference_molecule_exists:
                msg = 'No provided molecule matches topology molecule:\n'
                for index in sorted(list(molecule_graph)):
                    msg += 'Atom %8d %5s %5d %3s\n' % (atoms[index].index, atoms[index].name, atoms[index].residue.index, atoms[index].residue.name)
                raise Exception(msg)

    def _updateBondOrders(self, Wiberg = False):
        """Update and store list of bond orders for the molecules in this Topology. Can be used for initialization of bondorders list, or for updating bond orders in the list.

    Parameters:
    ----------
    Wiberg : bool (optional)
        Default False. If False, uses bond orders OEChem assigns to bonds on the molecule. If True, instead uses Wiberg bond orders stored on bonds in the molecule. These must already be present, i.e. from assignPartialCharges with an AM1 method.
"""
        # Initialize
        self._bondorders=list()
        self._bondorders_by_atomindices = {}
        # Loop over reference molecules and pull bond orders

        for mol in self._reference_molecules:
            # Pull mappings for this molecule
            mappings = self._reference_to_topology_atom_mappings[mol]
            # Loop over bonds
            for idx,bond in enumerate(mol.GetBonds()):
                # Get atom indices involved in bond
                at1 = bond.GetBgn().GetIdx()
                at2 = bond.GetEnd().GetIdx()
                # Get bond order
                if not Wiberg:
                    order = bond.GetOrder()
                else:
                    order = bond.GetData('WibergBondOrder')
                # Convert atom numbers to topology atom numbers; there may be multiple matches
                for mapping in mappings:
                    topat1 = None
                    topat2 = None
                    for mapatom in mapping:
                        if mapatom==at1:
                            topat1 = mapping[mapatom]
                        elif mapatom==at2:
                            topat2 = mapping[mapatom]
                    if topat1==None or topat2==None:
                        raise ValueError("No mapping found for these topology atoms (indices %s-%s)." % (at1, at2))
                    # Store bond order to re-use below and elsewhere; store in both directions
                    if not topat1 in self._bondorders_by_atomindices:
                        self._bondorders_by_atomindices[topat1] = {}
                    if not topat2 in self._bondorders_by_atomindices:
                        self._bondorders_by_atomindices[topat2] = {}
                    self._bondorders_by_atomindices[topat2][topat1] = order
                    self._bondorders_by_atomindices[topat1][topat2] = order

        # Loop over bonds in topology and store orders in the same order
        for bond in self._bonds:
            # See if we have in the 0-1 order and store
            topat1 = bond[0].index
            topat2 = bond[1].index
            order = self._bondorders_by_atomindices[topat1][topat2]
            self._bondorders.append(order)

    def unrollSMIRKSMatches(self, smirks, aromaticity_model = None):
        """Find all sets of atoms in the topology that match the provided SMIRKS strings.

        Parameters
        ----------
        smirks : str
            SMIRKS string with tagged atoms.
            If there are N tagged atoms numbered 1..N, the resulting matches will be N-tuples of atoms that match the corresponding tagged atoms.
        aromaticity_model : str (optional)
            Default None. Aromaticity model used in SMIRKS matching, as per getSMIRKSMatches_OEMol docs. If provided, pre-processes molecule with this model prior to matching. Otherwise, uses provided oemol.

        Returns
        -------
        matches : list of tuples of Atom
            matches[index] is an N-tuple of Atom entries from the topology
            Matches are returned in no guaranteed order.

        """

        # Perform matching on each unique molecule, unrolling the matches to all matching copies of that molecule in the Topology object.
        matches = list()
        for reference_molecule in self._reference_molecules:
            # Find all atomsets that match this definition in the reference molecule
            refmol_matches = getSMIRKSMatches_OEMol( reference_molecule, smirks, aromaticity_model = aromaticity_model)

            # Loop over matches
            for reference_atom_indices in refmol_matches:
                # Unroll corresponding atom indices over all instances of this molecule
                for reference_to_topology_atom_mapping in self._reference_to_topology_atom_mappings[reference_molecule]:
                    # Create match.
                    atom_indices = tuple([ reference_to_topology_atom_mapping[atom_index] for atom_index in reference_atom_indices ])
                    matches.append(atom_indices)

        return matches


#=============================================================================================
# FORCEFIELD
#=============================================================================================

# Enumerated values for nonbonded method

class NoCutoff(object):
    def __repr__(self):
        return 'NoCutoff'
NoCutoff = NoCutoff()

class CutoffNonPeriodic(object):
    def __repr__(self):
        return 'CutoffNonPeriodic'
CutoffNonPeriodic = CutoffNonPeriodic()

class CutoffPeriodic(object):
    def __repr__(self):
        return 'CutoffPeriodic'
CutoffPeriodic = CutoffPeriodic()

class Ewald(object):
    def __repr__(self):
        return 'Ewald'
Ewald = Ewald()

class PME(object):
    def __repr__(self):
        return 'PME'
PME = PME()

# Enumerated values for constraint type

class HBonds(object):
    def __repr__(self):
        return 'HBonds'
HBonds = HBonds()

class AllBonds(object):
    def __repr__(self):
        return 'AllBonds'
AllBonds = AllBonds()

class HAngles(object):
    def __repr__(self):
        return 'HAngles'
HAngles = HAngles()

# A map of functions to parse elements of the XML file.

parsers = {}

class ForceField(object):
    """A ForceField constructs OpenMM System objects based on a Topology.
    """

    def __init__(self, *files):
        """Load one or more XML parameter definition files and create a SMIRFF ForceField object based on them.

        Parameters
        ----------
        files : list
            A list of XML files defining the SMIRFF force field.
            Each entry may be an absolute file path, a path relative to the current working directory, a path relative to this module's data subdirectory (for built in force fields), or an open file-like object with a read() method from which the forcefield XML data can be loaded.

        """
        self._forces = []
        self.loadFile(files)

    def loadFile(self, files):
        """Load a SMIRFF XML file and add the definitions from it to this ForceField.

        Parameters
        ----------
        files : string or file or tuple
            An XML file or tuple of XML files containing SMIRFF force field definitions.
            Each entry may be an absolute file path, a path relative to the current working directory, a path relative to this module's data subdirectory (for built in force fields), or an open file-like object with a read() method from which the forcefield XML data can be loaded.
        """

        # Ensure that we are working with a tuple of files.
        if not isinstance(files, tuple):
            files = (files,)

        # Load in all XML trees.
        trees = list()
        for file in files:
            parser = etree.XMLParser(remove_blank_text=True) # For pretty print on write
            try:
                # this handles either filenames or open file-like objects
                tree = etree.parse(file, parser)
            except IOError:
                tree = etree.parse(os.path.join(os.path.dirname(__file__), 'data', file), parser)
            except Exception as e:
                # Fail with an error message about which file could not be read.
                # TODO: Also handle case where fallback to 'data' directory encounters problems,
                # but this is much less worrisome because we control those files.
                msg  = str(e) + '\n'
                if hasattr(file, 'name'):
                    filename = file.name
                else:
                    filename = str(file)
                msg += "ForceField.loadFile() encountered an error reading file '%s'\n" % filename
                raise Exception(msg)

            trees.append(tree)

        # Retain XML trees internally
        self._XMLTrees = trees
        # Store whether this has been modified or not; if modified, it will
        # trigger re-parsing/loading of XML on system creation
        self._XMLModified = False

        # Parse XML, get force definitions
        self.parseXMLTrees()

    def parseXMLTrees(self):
        """Parse XML trees, load force definitions."""

        trees = self._XMLTrees

        # We'll be creating all forces again from scratch by re-parsing
        self._forces = []

        # Store forcefield version info and, if present, aromaticity model
        root = trees[0].getroot()
        if root.tag=='SMIRFF':
            if 'version' in root.attrib:
                #TO DO: Should this be a float, a string, or something else?
                self.version = float(root.attrib['version'])
            else:
                self.version = 0.0
            if 'aromaticity_model' in root.attrib:
                self._aromaticity_model = root.attrib['aromaticity_model']
            else:
                self._aromaticity_model = None
        else:
            raise ValueError("Error: ForceField parses a SMIRFF forcefield, but this does not appear to be one as the root tag is %s." % root.tag)

        # Load force definitions
        for tree in trees:
            root = tree.getroot()

            # Before loading, do some error checking/consistency checking.
            # Warn if version number is not consistent
            if 'version' in root.attrib:
                if float(root.attrib['version']) != self.version:
                    print("Warning: Inconsistent version number in parsed FFXML files.")
            # Throw an exception if aromaticity model is not consistent
            if 'aromaticity_model' in root.attrib:
                if root.attrib['aromaticity_model'] != self._aromaticity_model:
                    raise ValueError("Error: Aromaticity model specified in FFXML files is inconsistent.")

            # Now actually load
            for child in root:
                if child.tag in parsers:
                    parsers[child.tag](child, self)


    def getGenerators(self):
        """Get the list of all registered generators."""
        return self._forces

    def registerGenerator(self, generator):
        """Register a new generator."""
        self._forces.append(generator)

    def getParameter(self, smirks = None, paramID=None, force_type='Implied'):
        """Get info associated with a particular parameter as specified by SMIRKS or parameter ID, and optionally force term.

    Parameters
    ----------
    smirks (optional) : str
        Default None. If specified, will pull parameters on line containing this `smirks`.
    paramID : str
        Default None. If specified, will pull parameters on line with this `id`
    force_type : str
        Default "Implied". Optionally, specify a particular force type such as
        "HarmonicBondForce" or "HarmonicAngleForce" etc. to search for a
        matching ID or SMIRKS.


    Returns
    -------
    params : dict
        Dictionary of attributes (parameters and their descriptions) from XML


Usage notes: SMIRKS or parameter ID must be specified.

To do: Update behavior of "Implied" force_type so it raises an exception if the parameter is not uniquely identified by the provided info.
"""
        # Check for valid input
        if smirks and paramID:
            raise ValueError("Error: Specify SMIRKS OR parameter ID but not both.")
        if smirks==None and paramID==None:
            raise ValueError("Error: Must specify SMIRKS or parameter ID.")


        trees=self._XMLTrees
        # Loop over XML files we read
        for tree in trees:
            # Loop over tree
            for child in tree.getroot():
                # Check a particular section?
                checksection = True
                if force_type is not 'Implied':
                    # See whether this has the tag we want to check
                    checksection= (child.tag==force_type)

                if checksection:
                    #Loop over descendants
                    for elem in child.iterdescendants(tag=etree.Element):
                        if (smirks and elem.attrib['smirks']==smirks) or (paramID and elem.attrib['id']==paramID):
                            return copy.deepcopy(elem.attrib)


    def setParameter(self, params, smirks=None, paramID=None, force_type="Implied"):
        """Get info associated with a particular parameter as specified by SMIRKS or parameter ID, and optionally force term.

    Parameters
    ----------
    params : dict
        Dictionary of attributes (parameters and their descriptions) for XML,
        i.e. as output by getParameter.
    smirks (optional) : str
        Default None. If specified, will set parameters on line containing this `smirks`.
    paramID (optional) : str
        Default None. If specified, will set parameters on line with this `id`
    force_type (optional) : str
        Default "Implied". Optionally, specify a particular force type such as
        "HarmonicBondForce" or "HarmonicAngleForce" etc. to search for a
        matching ID or SMIRKS.


    Returns
    -------
    status : bool
        True/False as to whether that parameter was found and successfully set

Usage notes: SMIRKS or parameter ID must be specified.

To do: Update behavior of "Implied" force_type so it raises an exception if the parameter is not uniquely identified by the provided info.
"""
        # Check for valid input
        if smirks and paramID:
            raise ValueError("Error: Specify SMIRKS OR parameter ID but not both.")
        if smirks==None and paramID==None:
            raise ValueError("Error: Must specify SMIRKS or parameter ID.")
        if not params:
            raise ValueError("Error, parameters must be specified.")

        # Below, we should do due dilegence that we're working on a parameter line which has
        # roughly the same types of parameters (though for torsions (or bonds, if we have partial bond orders),
        # the number of terms might differ), so define a utility function to give
        # back the basic names of parameters (i.e. k) without suffixes
        def get_param_names( param_keys ):
            names = set()
            for param in param_keys:
                ct = 1
                while param[-ct].isdigit():
                    ct+=1
                if ct > 1:
                    names.add( param[:-(ct-1)])
                else:
                    names.add( param )
            return names

        # Set parameters
        trees=self._XMLTrees
        status = False
        # Loop over XML files we read
        for tree in trees:
            # Loop over tree
            for child in tree.getroot():
                # Check a particular section?
                checksection = True
                if force_type is not 'Implied':
                    # See whether this has the tag we want to check
                    checksection= (child.tag==force_type)

                if checksection:
                    #Loop over descendants
                    for elem in child.iterdescendants(tag=etree.Element):
                        if (smirks and elem.attrib['smirks']==smirks) or (paramID and elem.attrib['id']==paramID):
                            # Try to set parameters
                            old_params=elem.attrib
                            if get_param_names(old_params.keys()) != get_param_names(params.keys()):
                                raise ValueError('Error: Provided parameters have different keys (%s) than existing parameters (%s).' % (', '.join(old_params.keys()), ', '.join(params.keys())))

                            # Loop over attributes, change values
                            for tag in params.keys():
                                elem.set( tag, params[tag])

                            # Found parameters and set, so update status
                            status = True


        # If we made any changes to XML, set flag so it will be reprocessed prior
        # to system creation
        if status:
            self._XMLModified = True

        return status

    def addParameter(self, params, smirks, force_type, tag):
        """Add specified SMIRKS/parameter in the section under the specified force type.

    Parameters
    ----------
    params : dict
        Dictionary of attributes (parameters and their descriptions) for XML,
        i.e. as output by getParameter.
    smirks : str
        SMIRKS pattern to associate with this parameter
    force_type : str
        Specify a particular force type such as "HarmonicBondForce" or "HarmonicAngleForce" in which to add this parameter
    tag : str
        Tag to use identifying this parameter, i.e. 'Bond' for a HarmonicBondForce, etc.


    Returns
    -------
    status : Bool
        Successful? True or False.


"""

        trees=self._XMLTrees
        # Loop over XML files we read
        found = False
        for tree in trees:
            # Loop over tree
            for child in tree.getroot():
                if child.tag==force_type:
                    found = True
                    addl = etree.Element( tag, smirks=smirks, attrib = params)
                    child.append(addl)
        return found


    def writeFile(self, files):
        """Write forcefield trees out to specified files."""

        # Ensure that we are working with a tuple of files.
        if not isinstance(files, tuple):
            files = (files,)

        for idx, filenm in enumerate(files):
            tree=self._XMLTrees[idx]
            tree.write( filenm, xml_declaration=True, pretty_print=True)

    def _assignPartialCharges(self, molecule, oechargemethod, modifycharges = True):
        """Assign partial charges to the specified molecule using best practices.

        Parameters
        ----------
        molecule : OEMol
            The molecule to be charged.
            NOTE: The molecule will be modified when charges are added.
        oechargemethod : str
            The name of the charge method from oequacpac to use (e.g. 'OECharges_AM1BCCSym')
        modifycharges : bool (optional)
            If False, don't actually assign partial charges; use the charge calculation solely to update the Wiberg bond orders.

        Notes:
        As per Christopher Bayly and http://docs.eyesopen.com/toolkits/cookbook/python/modeling/am1-bcc.html, OEAssignPartialCharges needs multiple conformations to ensure well-behaved charges. This implements that recipe for conformer generation.
        This conformer generation may or may not be necessary if the calculation is only to obtain bond orders; this will have to be investigated separately so it is retained for now.
        """
        # TODO: Cache charged molecules here to save time in future calls to createSystem

        # Expand conformers
        if not openeye.oechem.OEChemIsLicensed(): raise(ImportError("Need License for OEChem!"))
        if not openeye.oeomega.OEOmegaIsLicensed(): raise(ImportError("Need License for OEOmega!"))
        omega = openeye.oeomega.OEOmega()
        omega.SetMaxConfs(800)
        omega.SetCanonOrder(False)
        omega.SetSampleHydrogens(True)
        omega.SetEnergyWindow(15.0)
        omega.SetRMSThreshold(1.0)
        omega.SetStrictStereo(True) #Don't generate random stereoisomer if not specified
        charged_copy = openeye.oechem.OEMol(molecule)
        status = omega(charged_copy)
        if not status:
            raise(RuntimeError("Omega returned error code %s" % status))

        # Assign charges
        status = openeye.oequacpac.OEAssignPartialCharges(charged_copy, getattr(oequacpac, oechargemethod), False, False)
        if not status:
            raise(RuntimeError("OEAssignPartialCharges returned error code %s" % status))
        # Our copy has the charges we want but not the right conformation. Copy charges over. Also copy over Wiberg bond orders if present
        partial_charges = []
        partial_bondorders = []
        if modifycharges:
            for atom in charged_copy.GetAtoms():
                partial_charges.append( atom.GetPartialCharge() )
            for (idx,atom) in enumerate(molecule.GetAtoms()):
                atom.SetPartialCharge( partial_charges[idx] )
        for bond in charged_copy.GetBonds():
            partial_bondorders.append( bond.GetData("WibergBondOrder"))
        for (idx, bond) in enumerate(molecule.GetBonds()):
            bond.SetData("WibergBondOrder", partial_bondorders[idx])


    def createSystem(self, topology, molecules, nonbondedMethod=NoCutoff, nonbondedCutoff=1.0*unit.nanometer,
                     constraints=None, rigidWater=True, removeCMMotion=True, hydrogenMass=None, residueTemplates=dict(),
                     chargeMethod='BCC', verbose=False, **kwargs):
        """Construct an OpenMM System representing a Topology with this force field. XML will be re-parsed if it is modified prior to system creation.

        Parameters
        ----------
        topology : Topology
            The Topology for which to create a System
        molecules : list of openeye.oechem.OEMol
            List of molecules appearing in the topology
        nonbondedMethod : object=NoCutoff
            The method to use for nonbonded interactions.  Allowed values are
            NoCutoff, CutoffNonPeriodic, CutoffPeriodic, Ewald, or PME.
        nonbondedCutoff : distance=1*nanometer
            The cutoff distance to use for nonbonded interactions
        constraints : object=None
            Specifies which bonds and angles should be implemented with constraints.
            Allowed values are None, HBonds, AllBonds, or HAngles.
        rigidWater : boolean=True
            If true, water molecules will be fully rigid regardless of the value
            passed for the constraints argument
        removeCMMotion : boolean=True
            If true, a CMMotionRemover will be added to the System
        hydrogenMass : mass=None
            The mass to use for hydrogen atoms bound to heavy atoms.  Any mass
            added to a hydrogen is subtracted from the heavy atom to keep
            their total mass the same.
        residueTemplates : dict=dict()
           Key: Topology Residue object
           Value: string, name of _TemplateData residue template object to use for
                  (Key) residue
           This allows user to specify which template to apply to particular Residues
           in the event that multiple matching templates are available (e.g Fe2+ and Fe3+
           templates in the ForceField for a monoatomic iron ion in the topology).
        chargeModel : str, optional, default=None
           If 'BCC' is specified, bond charge corrections defined the `ForceField` will be applied to AM1-derived charges, otherwise charges from provided `molecules` will be used. (DEFAULT)
           If one of the `openeye.oequacpac.OECharges_` options is specified as a string (e.g. 'OECharges_AM1BCCSym'), this will be used and no bond charge corrections will be applied.
           If `None`, charges from the provided `molecules` will be used and no bond charge corrections will be applied.
        verbose : bool
           If True, verbose output will be printed.
        kwargs
             Arbitrary additional keyword arguments may also be specified.
             This allows extra parameters to be specified that are specific to
             particular force fields.

        Returns
        -------
        system
            the newly created System
        """
        # XML modified? If so, re-parse by generators
        if self._XMLModified:
            if verbose: print("Re-parsing XML because it was modified.")
            self.parseXMLTrees()

        # Make a deep copy of the input molecules so they are not modified by charging
        molecules = copy.deepcopy(molecules)

        # Charge molecules, if needed
        if chargeMethod == None:
            # Don't charge molecules
            if verbose: print('Charges specified in provided molecules will be used.')
            oechargemethod = None
        elif chargeMethod == 'BCC':
            # Check if we have a BondChargeCorrectionGenerator populated
            force_generators = { force.__class__.__name__ : force for force in self._forces }
            if ('BondChargeCorrectionGenerator' in force_generators):
                oechargemethod = force_generators['BondChargeCorrectionGenerator']._oechargemethod
                if verbose: print('Applying oechem.oequacpac.OEAssignPartialCharges with initial charge method "%s" followed by bond charge corrections.' % oechargemethod)
            else:
                # Don't charge molecules if no bond charge corrections were found
                oechargemethod = None
        elif type(chargeMethod) == str:
            # Check if the user has provided a manually-specified charge method
            if hasattr(oequacpac, chargeMethod):
                oechargemethod = chargeMethod
                if verbose: print('Applying oechem.oequacpac.OEAssignPartialCharges with specified charge method "%s".' % oechargemethod)
            else:
                raise Exception("Unknown chargeMethod '%s'"% chargeMethod)
        else:
            raise Exception("Unknown chargeMethod ''%s'"% str(chargeMethod))

        # Charge molecules if a valid charging method was identified
        if oechargemethod is not None:
            for molecule in molecules:
                self._assignPartialCharges(molecule, oechargemethod)

        # Work with a modified form of the topology that provides additional accessors.
        topology = _Topology(topology, molecules)

        # If the charge method was not an OpenEye AM1 method, obtain Wiberg bond orders
        if not (type(chargeMethod) == str and 'AM1' in chargeMethod):
            if verbose: print("Doing an AM1 calculation to get Wiberg bond orders.")
            for molecule in molecules:
                # Do AM1 calculation just to get bond orders on moleules (discarding charges)
                self._assignPartialCharges(molecule, "OECharges_AM1", modifycharges = False)


        # Update bond orders stored in the topology
        topology._updateBondOrders(Wiberg = True )

        # Create the System and add atoms
        system = openmm.System()
        for atom in topology.atoms():
            # Add the particle to the OpenMM system.
            system.addParticle(atom.element.mass) # TODO: Add option to use a different mass than the integral elemental mass?

        # Adjust hydrogen masses if requested.
        if hydrogenMass is not None:
            if not unit.is_quantity(hydrogenMass):
                hydrogenMass *= unit.dalton
            for atom1, atom2 in topology.bonds():
                if atom1.element == elem.hydrogen:
                    (atom1, atom2) = (atom2, atom1)
                if atom2.element == elem.hydrogen and atom1.element not in (elem.hydrogen, None):
                    transferMass = hydrogenMass-system.getParticleMass(atom2.index)
                    system.setParticleMass(atom2.index, hydrogenMass)
                    system.setParticleMass(atom1.index, system.getParticleMass(atom1.index)-transferMass)

        # Set periodic boundary conditions.
        boxVectors = topology.getPeriodicBoxVectors()
        if boxVectors is not None:
            system.setDefaultPeriodicBoxVectors(boxVectors[0], boxVectors[1], boxVectors[2])
        elif nonbondedMethod not in [NoCutoff, CutoffNonPeriodic]:
            raise ValueError('Requested periodic boundary conditions for a Topology that does not specify periodic box dimensions')

        # TODO: Convert requested bonds and angles to use constraints
        if constraints != None:
            raise Exception("Constraints are not implemented yet.")

        # Set nonbonded method.
        kwargs['nonbondedMethod'] = nonbondedMethod
        kwargs['nonbondedCutoff'] = nonbondedCutoff

        # Set user-specified charge method.
        kwargs['chargeMethod'] = chargeMethod

        # Add forces to the System
        for force in self._forces:
            if 'createForce' in dir(force):
                force.createForce(system, topology, verbose=verbose, **kwargs)

        # Add center-of-mass motion removal, if requested
        if removeCMMotion:
            system.addForce(openmm.CMMotionRemover())

        # Let force generators do postprocessing
        for force in self._forces:
            if 'postprocessSystem' in dir(force):
                force.postprocessSystem(system, topology, **kwargs)

        return system

    def labelMolecules(self, oemols, verbose = False):
        """Return labels for a list of OEMols corresponding to parameters from this force field. For each oemol, a dictionary of force types is returned, and for each force type, each force term is provided with the atoms involved, the parameter id assigned, and the corresponding SMIRKS.

        Parameters
        ----------
        oemols : list of OEMols
            The OpenEye OEChem OEMol objects as a list; these will be labeled. Should include all atoms with the correct ordering atom atom numbers will be returned along with corresponding labeling.
        verbose : bool
            If True, verbose output will be printed

        Returns
        -------
        molecule_labels : list
            list of labels for molecules. Each entry in the list corresponds to
            one molecule from the provided list of oemols and is a dictionary
            keyed by force type, i.e. molecule_labels[0]['HarmonicBondForce']
            gives details for the harmonic bond parameters for the first
            molecule. Each element is a list of the form [ ( [ atom1, ...,
            atomN], parameter_id, SMIRKS), ... ]

        """

        # XML modified? If so, re-parse by generators
        if self._XMLModified:
            if verbose: print("Re-parsing XML because it was modified.")
            self.parseXMLTrees()

        # Storage for labels
        molecule_labels = []

        # Loop over molecules and label
        for idx,mol in enumerate(oemols):
            molecule_labels.append({})
            for force in self._forces:
                # Initialize dictionary storage for this force type
                forcelabel = force.__class__.__name__

                # Grab force terms of this type for this molecule and store
                molecule_labels[idx][forcelabel] = force.labelForce( mol, verbose=verbose )
        return molecule_labels

#=============================================================================================
# The following classes are generators that know how to create Force subclasses and add them to a System that is being
# created.  Each generator class must define three methods: 1) a static method that takes an etree Element and a ForceField,
# and returns the corresponding generator object; 2) a createForce() method that constructs the Force object and adds it
# to the System; and 3) a labelForce() method that provides access to which
# terms are applied to which atoms in specified oemols.
# The static method should be added to the parsers map.
#=============================================================================================

def _validateSMIRKS(smirks, node=None):
    """Validate the specified SMIRKS string.

    Parameters
    ----------
    smirks : str
       The SMIRKS string to be validated
    node : xml.etree.ElementTree.Element
       Node of etree with 'sourceline' attribute.

    """
    qmol = oechem.OEQMol()
    if not oechem.OEParseSmarts(qmol, smirks):
        if (node is not None) and ('sourceline' in node.attrib):
            raise Exception("Line %s: Error parsing SMIRKS '%s'" % (node.attrib['sourceline'], node.attrib['smirks']))
        else:
            raise Exception("Error parsing SMIRKS '%s'" % (node.attrib['smirks']))

    return smirks

def _extractQuantity(node, parent, name, unit_name=None):
    """
    Form a (potentially unit-bearing) quantity from the specified attribute name.

    node : xml.etree.ElementTree.Element
       Node of etree corresponding to force type entry.
    parent : xml.etree.ElementTree.Element
       Node of etree corresponding to parent Force.
    name : str
       Name of parameter to extract from attributes.
    unit_name : str, optional, default=None
       If specified, use this attribute name of 'parent' to look up units

    """
    # Check for expected attributes
    if name not in node.attrib:
        if 'sourceline' in node.attrib:
            raise Exception("Line %d : Expected XML attribute '%s' not found" % (node.attrib['sourceline'], name))
        else:
            raise Exception("Expected XML attribute '%s' not found" % (name))

    # Most attributes will be converted to floats, but some are strings
    string_names = ['parent_id', 'id']
    # Handle case where this is a normal quantity
    if name not in string_names:
        quantity = float(node.attrib[name])
    # Handle label or string
    else:
        quantity = node.attrib[name]
        return quantity

    if unit_name is None:
        unit_name = name + '_unit'

    if unit_name in parent.attrib:
        # TODO: This is very dangerous.
        string = '(%s * %s).value_in_unit_system(md_unit_system)' % (node.attrib[name], parent.attrib[unit_name])
        quantity = eval(string, unit.__dict__)

    return quantity

import collections
class TransformedDict(collections.MutableMapping):
    """A dictionary that applies an arbitrary key-altering
       function before accessing the keys"""

    def __init__(self, *args, **kwargs):
        self.store = dict()
        self.update(dict(*args, **kwargs))  # use the free update to set keys

    def __getitem__(self, key):
        return self.store[self.__keytransform__(key)]

    def __setitem__(self, key, value):
        self.store[self.__keytransform__(key)] = value

    def __delitem__(self, key):
        del self.store[self.__keytransform__(key)]

    def __iter__(self):
        return iter(self.store)

    def __len__(self):
        return len(self.store)

    def __keytransform__(self, key):
        return key

class ValenceDict(TransformedDict):
    """Enforce uniqueness in atom indices"""
    def __keytransform__(self, key):
        """Reverse tuple if first element is larger than last element."""
        # Ensure key is a tuple.
        key = tuple(key)
        # Reverse the key if the first element is bigger than the last.
        if key[0] > key[-1]:
            key = tuple(reversed(key))
        return key

#=============================================================================================
# Force generators
#=============================================================================================

## @private
class HarmonicBondGenerator(object):
    """A HarmonicBondGenerator constructs a HarmonicBondForce."""

    class BondType(object):
        """A SMIRFF bond type."""
        def __init__(self, node, parent):
            self.smirks = _validateSMIRKS(node.attrib['smirks'], node=node)
            self.pid = _extractQuantity(node, parent, 'id')

            # Determine if we are using fractional bond orders for this bond
            # First, check if this force uses fractional bond orders
            if 'fractional_bondorder' in parent.attrib:
                # If it does, see if this parameter line provides fractional bond order parameters
                if 'length_bondorder1' in node.attrib and 'k_bondorder1' in node.attrib:
                    # Store what interpolation scheme we're using
                    self.fractional_bondorder = parent.attrib['fractional_bondorder']
                    # Store bondorder1 and bondorder2 parameters
                    self.k = list()
                    self.length = list()
                    for ct in range(1,3):
                        self.length.append( _extractQuantity(node, parent, 'length_bondorder%s' % ct, unit_name = 'length_unit') )
                        self.k.append( _extractQuantity(node, parent, 'k_bondorder%s' % ct, unit_name = 'k_unit') )
                else:
                    self.fractional_bondorder = None
            else:
                self.fractional_bondorder = None

            # If no fractional bond orders, just get normal length and k
            if self.fractional_bondorder == None:
                self.length = _extractQuantity(node, parent, 'length')
                self.k = _extractQuantity(node, parent, 'k')

    def __init__(self, forcefield):
        self.ff = forcefield
        self._bondtypes = list()

    def registerBond(self, node, parent):
        """Register a SMIRFF bondtype definition."""
        bond = HarmonicBondGenerator.BondType(node, parent)
        self._bondtypes.append(bond)

    @staticmethod
    def parseElement(element, ff):
        # Find existing force generator or create new one.
        existing = [f for f in ff._forces if isinstance(f, HarmonicBondGenerator)]
        if len(existing) == 0:
            generator = HarmonicBondGenerator(ff)
            ff.registerGenerator(generator)
        else:
            generator = existing[0]

        # Register all SMIRFF bond definitions.
        for bond in element.findall('Bond'):
            generator.registerBond(bond, element)

    def createForce(self, system, topology, verbose=False, **kwargs):
        # Find existing force or create new one.
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == openmm.HarmonicBondForce]
        if len(existing) == 0:
            force = openmm.HarmonicBondForce()
            system.addForce(force)
        else:
            force = existing[0]

        # Iterate over all defined bond types, allowing later matches to override earlier ones.
        bonds = ValenceDict()
        bondorders = ValenceDict()
        for bond in self._bondtypes:
            for atom_indices in topology.unrollSMIRKSMatches(bond.smirks, aromaticity_model = self.ff._aromaticity_model):
                bonds[atom_indices] = bond
                # Retrieve bond orders
                bondorders[atom_indices] = topology._bondorders_by_atomindices[atom_indices[0]][atom_indices[1]]

        if verbose:
            print('')
            print('HarmonicBondGenerator:')
            print('')
            for bond in self._bondtypes:
                print('%64s : %8d matches' % (bond.smirks, len(topology.unrollSMIRKSMatches(bond.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all bonds to the system.
        for (atom_indices, bond) in bonds.items():
            if bond.fractional_bondorder==None:
                force.addBond(atom_indices[0], atom_indices[1], bond.length, bond.k)
            # If this bond uses partial bond orders
            else:
                order = bondorders[atom_indices]
                if bond.fractional_bondorder=='interpolate-linear':
                    k = bond.k[0] + (bond.k[1]-bond.k[0])*(order-1.)
                    length = bond.length[0] + (bond.length[1]-bond.length[0])*(order-1.)
                    force.addBond(atom_indices[0], atom_indices[1], length, k)
                    if verbose: print("%64s" % "Added %s bond, order %.2f; length=%.2g; k=%.2g" % (bond.smirks, order, length, k))
                else:
                    raise Exception("Partial bondorder treatment %s is not implemented." % bond.fractional_bondorder)

        if verbose: print('%d bonds added' % (len(bonds)))


        # Check that no topology bonds are missing force parameters
        atoms = [ atom for atom in topology.atoms() ]
        topology_bonds = ValenceDict()
        for (atom1, atom2) in topology.bonds():
            topology_bonds[(atom1.index,atom2.index)] = True
        if set(bonds.keys()) != set(topology_bonds.keys()):
            msg = 'Mismatch between bonds added and topological bonds.\n'
            created_bondset = set(bonds.keys())
            topology_bondset = set(topology_bonds.keys())
            msg += 'Bonds created that are not present in Topology:\n'
            msg += str(created_bondset.difference(topology_bondset)) + '\n'
            msg += 'Topology bonds not assigned parameters:\n'
            for (a1, a2) in topology_bondset.difference(created_bondset):
                atom1 = atoms[a1]
                atom2 = atoms[a2]
                msg += '(%8d,%8d) : %5s %3s %3s - %5s %3s %3s' % (a1, a2, atom1.residue.index, atom1.residue.name, atom1.name, atom2.residue.index, atom2.residue.name, atom2.name)
                msg += '\n'
            raise Exception(msg)


    def labelForce(self, oemol, verbose=False, **kwargs):
        """Take a provided OEMol and parse HarmonicBondForce terms for this molecule.

        Parameters
        ----------
            oemol : OEChem OEMol object for molecule to be examined

        Returns
        ---------
            force_terms: list
                Returns a list of tuples, [ ([atom id 1, ... atom id N], parameter id, smirks) , (....), ... ] for all forces of this type which would be applied.
        """

        # Iterate over all defined bond SMIRKS, allowing later matches to override earlier ones.
        bonds = ValenceDict()
        for bond in self._bondtypes:
            for atom_indices in getSMIRKSMatches_OEMol( oemol, bond.smirks, aromaticity_model = self.ff._aromaticity_model ):
                bonds[atom_indices] = bond

        if verbose:
            print('')
            print('HarmonicBondGenerator:')
            print('')
            for bond in self._bondtypes:
                print('%64s : %8d matches' % (bond.smirks, len(getSMIRKSMatches_OEMol(oemol, bond.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all bonds to the output list
        force_terms = []
        for (atom_indices, bond) in bonds.items():
            force_terms.append( ([atom_indices[0], atom_indices[1]], bond.pid, bond.smirks) )

        return force_terms

parsers["HarmonicBondForce"] = HarmonicBondGenerator.parseElement

#=============================================================================================

## @private
class HarmonicAngleGenerator(object):
    """A HarmonicAngleGenerator constructs a HarmonicAngleForce."""

    class AngleType(object):
        """A SMIRFF angle type."""
        def __init__(self, node, parent):
            self.smirks = _validateSMIRKS(node.attrib['smirks'], node=node)
            self.angle = _extractQuantity(node, parent, 'angle')
            self.k = _extractQuantity(node, parent, 'k')
            self.pid = _extractQuantity(node, parent, 'id')
            if 'fractional_bondorder' in parent.attrib:
                self.fractional_bondorder = parent.attrib['fractional_bondorder']
            else:
                self.fractional_bondorder = None

    def __init__(self, forcefield):
        self.ff = forcefield
        self._angletypes = list()

    def registerAngle(self, node, parent):
        """Register a SMIRFF angletype definition."""
        angle = HarmonicAngleGenerator.AngleType(node, parent)
        self._angletypes.append(angle)

    @staticmethod
    def parseElement(element, ff):
        # Find existing force generator or create new one.
        existing = [f for f in ff._forces if isinstance(f, HarmonicAngleGenerator)]
        if len(existing) == 0:
            generator = HarmonicAngleGenerator(ff)
            ff.registerGenerator(generator)
        else:
            generator = existing[0]

        # Register all SMIRFF angle definitions.
        for angle in element.findall('Angle'):
            generator.registerAngle(angle, element)

    def createForce(self, system, topology, verbose=False, **kwargs):
        # Find existing force or create new one.
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == openmm.HarmonicAngleForce]
        if len(existing) == 0:
            force = openmm.HarmonicAngleForce()
            system.addForce(force)
        else:
            force = existing[0]

        # Iterate over all defined angle types, allowing later matches to override earlier ones.
        angles = ValenceDict()
        for angle in self._angletypes:
            for atom_indices in topology.unrollSMIRKSMatches(angle.smirks, aromaticity_model = self.ff._aromaticity_model):
                angles[atom_indices] = angle

        if verbose:
            print('')
            print('HarmonicAngleGenerator:')
            print('')
            for angle in self._angletypes:
                print('%64s : %8d matches' % (angle.smirks, len(topology.unrollSMIRKSMatches(angle.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all angles to the system.
        for (atom_indices, angle) in angles.items():
            force.addAngle(atom_indices[0], atom_indices[1], atom_indices[2], angle.angle, angle.k)

        if verbose: print('%d angles added' % (len(angles)))


    def labelForce(self, oemol, verbose=False, **kwargs):
        """Take a provided OEMol and parse HarmonicAngleForce terms for this molecule.

        Parameters
        ----------
            oemol : OEChem OEMol object for molecule to be examined

        Returns
        ---------
            force_terms: list
                Returns a list of tuples, [ ([atom id 1, ... atom id N], parameter id, smirks) , (....), ... ] for all forces of this type which would be applied.
        """

        # Iterate over all defined angle types, allowing later matches to override earlier ones.
        angles = ValenceDict()
        for angle in self._angletypes:
            for atom_indices in getSMIRKSMatches_OEMol(oemol, angle.smirks, aromaticity_model = self.ff._aromaticity_model):
                angles[atom_indices] = angle

        if verbose:
            print('')
            print('HarmonicAngleGenerator:')
            print('')
            for angle in self._angletypes:
                print('%64s : %8d matches' % (angle.smirks, len(getSMIRKSMatches_OEMol(oemol, angle.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all angles to the output list
        force_terms = []
        for (atom_indices, angle) in angles.items():
            force_terms.append( ([atom_indices[0], atom_indices[1], atom_indices[2]], angle.pid, angle.smirks) )

        return force_terms

parsers["HarmonicAngleForce"] = HarmonicAngleGenerator.parseElement


#=============================================================================================

## @private
class PeriodicTorsionGenerator(object):
    """A PeriodicTorsionForceGenerator constructs a PeriodicTorsionForce."""

    class TorsionType(object):

        """A SMIRFF torsion type."""
        def __init__(self, node, parent):
            self.smirks = _validateSMIRKS(node.attrib['smirks'], node=node)
            self.periodicity = list()
            self.phase = list()
            self.k = list()
            self.pid = _extractQuantity(node, parent, 'id')
            self.torsiontype = node.tag #Improper or Proper?

            # Check that the SMIRKS pattern matches the type it's supposed
            # to be (avoiding bugs wherein an improperly formed generic improper
            # overrides propers, for example)
            try:
                chemenv = env.ChemicalEnvironment(self.smirks)
                thistype = chemenv.getType()
                if thistype=='Torsion': thistype = 'Proper'
                if self.torsiontype != thistype:
                    raise Exception("Error: SMIRKS pattern %s (parameter %s) does not specify a %s torsion, but it is supposed to." % (self.smirks, self.pid, self.torsiontype))
            except env.SMIRKSParsingError:
                print("Warning: Could not confirm whether smirks pattern %s is a valid %s torsion." % (self.smirks, self.torsiontype))


            if 'fractional_bondorder' in parent.attrib:
                self.fractional_bondorder = parent.attrib['fractional_bondorder']
            else:
                self.fractional_bondorder = None
            # Store parameters.
            index = 1
            while 'phase%d'%index in node.attrib:
                self.periodicity.append( int(_extractQuantity(node, parent, 'periodicity%d' % index)) )
                self.phase.append( _extractQuantity(node, parent, 'phase%d' % index, unit_name='phase_unit') )
                self.k.append( _extractQuantity(node, parent, 'k%d' % index, unit_name='k_unit') )
                # Optionally handle 'idivf', which divides the periodicity by the specified value
                if ('idivf%d' % index) in node.attrib:
                    idivf = _extractQuantity(node, parent, 'idivf%d' % index)
                    self.k[-1] /= float(idivf)
                index += 1
                # SMIRFF applies trefoil (six-fold) impropers unlike AMBER
                # If it's an improper, divide by the factor of six internally
                if self.torsiontype=='Improper':
                    self.k[-1] /= 6.

    def __init__(self, forcefield):
        self.ff = forcefield
        self._torsiontypes = list()

    def registerTorsion(self, node, parent):
        """Register a SMIRFF torsiontype definition."""
        torsion = PeriodicTorsionGenerator.TorsionType(node, parent)
        self._torsiontypes.append(torsion)

    @staticmethod
    def parseElement(element, ff):
        # Find existing force generator or create new one.
        existing = [f for f in ff._forces if isinstance(f, PeriodicTorsionGenerator)]
        if len(existing) == 0:
            generator = PeriodicTorsionGenerator(ff)
            ff.registerGenerator(generator)
        else:
            generator = existing[0]

        # Register all SMIRFF torsion definitions.
        for torsion in element.findall('Proper'):
            generator.registerTorsion(torsion, element)
        for torsion in element.findall('Improper'):
            generator.registerTorsion(torsion, element)

    def createForce(self, system, topology, verbose=False, **kwargs):
        # Find existing force or create new one.
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == openmm.PeriodicTorsionForce]
        if len(existing) == 0:
            force = openmm.PeriodicTorsionForce()
            system.addForce(force)
        else:
            force = existing[0]

        # Iterate over all defined torsion types, allowing later matches to override earlier ones.
        torsions = ValenceDict()
        for torsion in self._torsiontypes:
            for atom_indices in topology.unrollSMIRKSMatches(torsion.smirks, aromaticity_model = self.ff._aromaticity_model):
                torsions[atom_indices] = torsion

        if verbose:
            print('')
            print('PeriodicTorsionGenerator:')
            print('')
            for torsion in self._torsiontypes:
                print('%64s : %8d matches' % (torsion.smirks, len(topology.unrollSMIRKSMatches(torsion.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all torsions to the system.
        for (atom_indices, torsion) in torsions.items():
            for (periodicity, phase, k) in zip(torsion.periodicity, torsion.phase, torsion.k):
                force.addTorsion(atom_indices[0], atom_indices[1], atom_indices[2], atom_indices[3], periodicity, phase, k)

        if verbose: print('%d torsions added' % (len(torsions)))

    def labelForce(self, oemol, verbose=False, **kwargs):
        """Take a provided OEMol and parse PeriodicTorsionForce terms for this molecule.

        Parameters
        ----------
            oemol : OEChem OEMol object for molecule to be examined

        Returns
        ---------
            force_terms: list
                Returns a list of tuples, [ ([atom id 1, ... atom id N], parameter id, smirks) , (....), ... ] for all forces of this type which would be applied.
        """


        # Iterate over all defined torsion types, allowing later matches to override earlier ones.
        torsions = ValenceDict()
        for torsion in self._torsiontypes:
            for atom_indices in getSMIRKSMatches_OEMol(oemol, torsion.smirks, aromaticity_model = self.ff._aromaticity_model):
                torsions[atom_indices] = torsion

        if verbose:
            print('')
            print('PeriodicTorsionGenerator:')
            print('')
            for torsion in self._torsiontypes:
                print('%64s : %8d matches' % (torsion.smirks, len(getSMIRKSMatches_OEMol(oemol, torsion.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all torsions to the output list
        force_terms = []
        for (atom_indices, torsion) in torsions.items():
            force_terms.append( ([atom_indices[0], atom_indices[1], atom_indices[2], atom_indices[3]], torsion.pid, torsion.smirks) )

        return force_terms

parsers["PeriodicTorsionForce"] = PeriodicTorsionGenerator.parseElement

## @private
class NonbondedGenerator(object):
    """A NonbondedGenerator constructs a NonbondedForce."""

    SCALETOL = 1e-5

    class LennardJonesType(object):
        """A SMIRFF Lennard-Jones type."""
        def __init__(self, node, parent):
            """Currently we support radius definition via 'sigma' or 'rmin_half'."""
            self.smirks = _validateSMIRKS(node.attrib['smirks'], node=node)
            self.pid = _extractQuantity(node, parent, 'id')
            if 'fractional_bondorder' in parent.attrib:
                self.fractional_bondorder = parent.attrib['fractional_bondorder']
            else:
                self.fractional_bondorder = None

            # Make sure we don't have BOTH rmin_half AND sigma
            try:
                a = _extractQuantity(node, parent, 'sigma')
                a = _extractQuantity(node, parent, 'rmin_half')
                raise Exception("Error: BOTH sigma and rmin_half cannot be specified simultaneously in the .ffxml file.")
            except:
                pass

            #Handle sigma
            try:
                self.sigma = _extractQuantity(node, parent, 'sigma')
            #Handle rmin_half, AMBER-style
            except:
                rmin_half = _extractQuantity(node, parent, 'rmin_half', unit_name='sigma_unit')
                self.sigma = 2.*rmin_half/(2.**(1./6.))
            self.epsilon = _extractQuantity(node, parent, 'epsilon')

    def __init__(self, forcefield, coulomb14scale, lj14scale):
        self.ff = forcefield
        self.coulomb14scale = coulomb14scale
        self.lj14scale = lj14scale
        self._ljtypes = list()

    def registerAtom(self, node, parent):
        ljtype = NonbondedGenerator.LennardJonesType(node, parent)
        self._ljtypes.append(ljtype)

    @staticmethod
    def parseElement(element, ff):
        existing = [f for f in ff._forces if isinstance(f, NonbondedGenerator)]
        if len(existing) == 0:
            generator = NonbondedGenerator(ff, float(element.attrib['coulomb14scale']), float(element.attrib['lj14scale']))
            ff.registerGenerator(generator)
        else:
            # Multiple <NonbondedForce> tags were found, probably in different files.  Simply add more types to the existing one.
            generator = existing[0]
            if abs(generator.coulomb14scale - float(element.attrib['coulomb14scale'])) > NonbondedGenerator.SCALETOL or \
                    abs(generator.lj14scale - float(element.attrib['lj14scale'])) > NonbondedGenerator.SCALETOL:
                raise ValueError('Found multiple NonbondedForce tags with different 1-4 scales')
        for atom in element.findall('Atom'):
            generator.registerAtom(atom, element)

    def createForce(self, system, topology, nonbondedMethod=NoCutoff, nonbondedCutoff=0.9, verbose=False, **args):
        methodMap = {NoCutoff:openmm.NonbondedForce.NoCutoff,
                     CutoffNonPeriodic:openmm.NonbondedForce.CutoffNonPeriodic,
                     CutoffPeriodic:openmm.NonbondedForce.CutoffPeriodic,
                     Ewald:openmm.NonbondedForce.Ewald,
                     PME:openmm.NonbondedForce.PME}
        if nonbondedMethod not in methodMap:
            raise ValueError('Illegal nonbonded method for NonbondedForce')
        force = openmm.NonbondedForce()
        force.setNonbondedMethod(methodMap[nonbondedMethod])
        force.setCutoffDistance(nonbondedCutoff)
        if 'ewaldErrorTolerance' in args:
            force.setEwaldErrorTolerance(args['ewaldErrorTolerance'])
        if 'useDispersionCorrection' in args:
            force.setUseDispersionCorrection(bool(args['useDispersionCorrection']))
        system.addForce(force)

        # Iterate over all defined Lennard-Jones types, allowing later matches to override earlier ones.
        atoms = ValenceDict()
        for ljtype in self._ljtypes:
            for atom_indices in topology.unrollSMIRKSMatches(ljtype.smirks, aromaticity_model = self.ff._aromaticity_model):
                atoms[atom_indices] = ljtype

        if verbose:
            print('')
            print('NonbondedForceGenerator:')
            print('')
            for ljtype in self._ljtypes:
                print('%64s : %8d matches' % (ljtype.smirks, len(topology.unrollSMIRKSMatches(ljtype.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all Lennard-Jones terms to the system.
        # Create all particles.
        for atom in topology.atoms():
            force.addParticle(0.0, 1.0, 0.0)
        # Set the particle Lennard-Jones terms.
        for (atom_indices, ljtype) in atoms.items():
            force.setParticleParameters(atom_indices[0], 0.0, ljtype.sigma, ljtype.epsilon)

        # Set the partial charges based on reference molecules.
        for reference_molecule in topology._reference_molecules:
            atom_mappings = topology._reference_to_topology_atom_mappings[reference_molecule]
            for atom_mapping in atom_mappings:
                for (atom, atom_index) in zip(reference_molecule.GetAtoms(), atom_mapping):
                    [charge, sigma, epsilon] = force.getParticleParameters(atom_index)
                    force.setParticleParameters(atom_index, atom.GetPartialCharge(), sigma, epsilon)

    def postprocessSystem(self, system, topology, verbose=False, **args):
        atoms = [ atom for atom in topology.atoms() ]
        natoms = len(atoms)

        # Create exceptions based on bonds.
        bondIndices = []
        for (atom1, atom2) in topology.bonds():
            if (atom1.index < 0) or (atom2.index < 0) or (atom1.index >= natoms) or (atom2.index >= natoms):
                raise Exception('atom indices out of bounds')
            bondIndices.append((atom1.index, atom2.index))

        # Create the exceptions.
        nonbonded = [f for f in system.getForces() if isinstance(f, openmm.NonbondedForce)][0]
        nonbonded.createExceptionsFromBonds(bondIndices, self.coulomb14scale, self.lj14scale)

    def labelForce(self, oemol, verbose=False, **kwargs):
        """Take a provided OEMol and parse HarmonicBondForce terms for this molecule.

        Parameters
        ----------
            oemol : OEChem OEMol object for molecule to be examined

        Returns
        ---------
            force_terms: list
                Returns a list of tuples, [ ([atom id 1, ... atom id N], parameter id, smirks) , (....), ... ] for all forces of this type which would be applied.
        """

        # Iterate over all defined Lennard-Jones types, allowing later matches to override earlier ones.
        atoms = ValenceDict()
        for ljtype in self._ljtypes:
            for atom_indices in getSMIRKSMatches_OEMol(oemol, ljtype.smirks, aromaticity_model = self.ff._aromaticity_model):
                atoms[atom_indices] = ljtype

        if verbose:
            print('')
            print('NonbondedForceGenerator:')
            print('')
            for ljtype in self._ljtypes:
                print('%64s : %8d matches' % (ljtype.smirks, len(getSMIRKSMatches_OEMol(oemol, ljtype.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all Lennard-Jones terms to the output list
        force_terms = []
        for (atom_indices, ljtype) in atoms.items():
            force_terms.append( ([atom_indices[0]], ljtype.pid, ljtype.smirks) )

        return force_terms


parsers["NonbondedForce"] = NonbondedGenerator.parseElement

## @private
class BondChargeCorrectionGenerator(object):
    """A BondChargeCorrectionGenerator handles <BondChargeCorrections>."""

    class BondChargeCorrectionType(object):
        """A SMIRFF bond type."""
        def __init__(self, node, parent):
            self.smirks = _validateSMIRKS(node.attrib['smirks'], node=node)
            self.increment = _extractQuantity(node, parent, 'increment')
            self.pid = _extractQuantity(node, parent, 'id')
            # If no units are specified, assume elementary charge
            if type(self.increment) == float:
                self.increment *= unit.elementary_charge

    def __init__(self, forcefield, initialChargeMethod):
        self.ff = forcefield
        self._bondChargeCorrections = list()
        self._initialChargeMethod = initialChargeMethod
        self._oechargemethod = 'OECharges_' + initialChargeMethod
        if not hasattr(oequacpac, self._oechargemethod):
            raise Exception("BondChargeCorrectionGenerator: initialChargeMethod attribute was '%s', but '%s' was not found in oequacpac available methods." % (initialChargeMethod, self._oechargemethod))

    def registerBondChargeCorrection(self, node, parent):
        """Register a SMIRFF bondtype definition."""
        bond = BondChargeCorrectionGenerator.BondChargeCorrectionType(node, parent)
        self._bondChargeCorrections.append(bond)

    @staticmethod
    def parseElement(element, ff):
        # Find existing force generator or create new one.
        existing = [f for f in ff._forces if isinstance(f, BondChargeCorrectionGenerator)]
        if len(existing) == 0:
            generator = BondChargeCorrectionGenerator(ff, element.attrib['method'])
            ff.registerGenerator(generator)
        else:
            # Check that new bond charge generator doesn't request a different method from existing one.
            if element.attrib['method'] != existing[0]._initialChargeMethod:
                raise Exception("Existing BondChargeCorrectionGenerator uses initial charging method '%s' while new BondChargeCorrectionGenerator requests '%s'" % (existing[0]._initialChargeMethod, element.attrib['method']))

            generator = existing[0]

        # Register all SMIRFF bond definitions.
        for bond in element.findall('BondChargeCorrection'):
            generator.registerBondChargeCorrection(bond, element)

    def createForce(self, system, topology, verbose=False, **args):
        # No forces are created by this generator.
        pass

    def labelForce(self, oemol, verbose=False, **kwargs):
        """Take a provided OEMol and parse BondChargeCorrection terms for this molecule.

        Parameters:
        -----------
        oemol : OEChem OEMol object for molecule to be examined

        Returns
        -------
        parameter_terms : list
            Returns a list of tuples, [ ([atom id 1, atom id 2], parameter id, smirks), (...), ...] for all sets of atoms to which this parameter would be applied.
        """
        bccs = {}
        for bcc in self._bondChargeCorrections:
            for atom_indices in getSMIRKSMatches_OEMol(oemol, bcc.smirks, aromaticity_model = self.ff._aromaticity_model):
                bccs[atom_indices] = bcc

        if verbose:
            print('')
            print('BondChargeCorrectionGenerator:')
            print('')
            for bcc in self._bondChargeCorrections:
                print('%64s : %8d matches' % (bcc.smirks, len(getSMIRKSMatches_OEMol(oemol, bcc.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Add all BCCs to the output list
        force_terms = []
        for (atom_indices, bcc) in bccs.items():
            force_terms.append( ([atom_indices[0], atom_indices[1]], bcc.pid, bcc.smirks) )

        return force_terms

    def postprocessSystem(self, system, topology, verbose=False, chargeMethod=None, **args):
        if chargeMethod != 'BCC':
            # Only apply charge corrections if chargeMethod is 'BCC'
            return

        # Iterate over all defined bond charge corrections, allowing later matches to override earlier ones.
        bonds = ValenceDict()
        for bond in self._bondChargeCorrections:
            for atom_indices in topology.unrollSMIRKSMatches(bond.smirks, aromaticity_model = self.ff._aromaticity_model):
                bonds[atom_indices] = bond

        if verbose:
            print('')
            print('Bond charge corrections:')
            print('')
            for bond in self._bondChargeCorrections:
                print('%64s %12.6f : %8d matches' % (bond.smirks, bond.increment / unit.elementary_charge, len(topology.unrollSMIRKSMatches(bond.smirks, aromaticity_model = self.ff._aromaticity_model))))
            print('')

        # Apply bond charge increments
        for force in system.getForces():
            if force.__class__.__name__ in ['NonbondedForce']:
                for (atom_indices, bond) in bonds.items():
                    # Retrieve parameters
                    [charge0, sigma0, epsilon0] = force.getParticleParameters(atom_indices[0])
                    [charge1, sigma1, epsilon1] = force.getParticleParameters(atom_indices[1])
                    # Apply bond charge increment
                    charge0 -= bond.increment
                    charge1 += bond.increment
                    # Update charges
                    force.setParticleParameters(atom_indices[0], charge0, sigma0, epsilon0)
                    force.setParticleParameters(atom_indices[1], charge1, sigma1, epsilon1)

parsers["BondChargeCorrections"] = BondChargeCorrectionGenerator.parseElement
