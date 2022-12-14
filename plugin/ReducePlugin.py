import nanome
import tempfile
import os
import sys
import math

from nanome.api.ui import Menu
from nanome.util import async_callback, Logs, Process, Vector3, enums
from nanome.api.structure import Complex
from nanome._internal._structure import _Bond

BASE_DIR = os.path.join(os.path.dirname(__file__))

class ReducePlugin(nanome.AsyncPluginInstance):

    def start(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.input_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdb', dir=self.temp_dir.name)
        self.output_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdb', dir=self.temp_dir.name)
        self.reduce_flip = True
        self.reduce_HIS = False


    @async_callback
    async def on_run(self):
        complex_indices = [comp.index for comp in await self.request_complex_list()]
        deep = await self.request_complexes(complex_indices)
        result = await self.add_hydrogens(complexes=deep)
        await self.update_structures_deep(result)

    @async_callback
    async def add_hydrogens(self, request=None, complexes=None):
        if request:
            complexes = request.get_args()

        for complex in complexes:
            complex.io.to_pdb(self.input_file.name)

            # remember atoms by position
            atom_by_position = dict()
            for atom in complex.atoms:
                atom_by_position[get_position_key(atom)] = atom

            # compute all hydrogens
            result_complex, hydrogen_ids = await call_Reduce(self.input_file.name, self.output_file, self.reduce_flip, self.reduce_HIS)
            if result_complex == -1:
                self.send_notification(enums.NotificationTypes.error, 'Error computing hydrogens, reduce failed')
                continue
            if not result_complex or len(hydrogen_ids) == 0:
                Logs.error("Could not read the PDB generated by Reduce")
                continue

            # add hydrogens to original complex
            self.match_and_update(atom_by_position, result_complex, hydrogen_ids)

        if request:
            request.send_response(complexes)

        return complexes


    def match_and_update(self, atom_by_position, result_complex, hydrogen_ids):
        """
        Match and add hydrogens to original complex using positions to match.

        :param atom_by_position: dict mapping position key to atom in source complex
        :type atom_by_position: dict
        :param result_complex: Output complex from hydrogens calculation
        :type result_complex: :class:`nanome.structure.Complex`
        :param hydrogen_ids: List of hydrogen atom ids to use
        :type hydrogen_ids: list of int
        """
        Logs.debug("Matching atoms between Reduce-generated file and Nanome structure")
        atoms = list(result_complex.atoms)
        for id in hydrogen_ids:
            h_atom = atoms[id]
            h_atom.symbol = "H"
            
            #Need to find the atom bonded to this hydrogen
            bonded_atom, dist = self.get_closest_heavy_atom_in_residue(result_complex, h_atom)

            if not bonded_atom:
                Logs.debug("Could not find heavy atom linked to the hydrogen", id)
                continue
            
            if dist > 2.0:
                Logs.debug("Could not find heavy atom linked to the hydrogen", id, "(too far)")
                continue

            bond = self.add_bond(h_atom, bonded_atom)

            bonded_atom_key = get_position_key(bonded_atom)

            if bonded_atom_key is None or bonded_atom_key not in atom_by_position:
                Logs.warning(f'H {h_atom.serial} bonded with unknown atom {bonded_atom.symbol} at {bonded_atom.position}')
                continue

            source_atom = atom_by_position[bonded_atom_key]

            new_atom = h_atom._shallow_copy()
            new_atom._display_mode = source_atom._display_mode
            new_atom.is_het = source_atom.is_het
            new_atom.selected = source_atom.selected

            # copy bfactor and occupancy for surface coloring
            new_atom.bfactor = source_atom.bfactor
            new_atom.occupancy = source_atom.occupancy

            new_bond = bond._shallow_copy()
            new_bond.atom1 = source_atom
            new_bond.atom2 = new_atom

            residue = source_atom.residue
            residue.add_atom(new_atom)
            residue.add_bond(new_bond)
        
        Logs.debug("Matching done")

    def add_bond(self, atom1, atom2):
        new_bond = _Bond._create()
        new_bond._kind = nanome.util.enums.Kind.CovalentSingle
        new_bond._atom1 = atom1
        new_bond._atom2 = atom2
        atom2.residue._add_bond(new_bond)
        return new_bond

    def get_closest_heavy_atom_in_residue(self, complex, atom):
        result = None
        minD = 10000.0
        for a in atom.residue.atoms:
            if a != atom and a.symbol != "H":
                d = Vector3.distance(a.position, atom.position)
                if d < minD:
                    result = a
                    minD = d
        return result, minD

def get_position_key(atom):
    """
    Get atom position as tuple of integers to be used as lookup key.
    Rounds the coordinates to 4 decimal places before multiplying by 50
    to get unique integer-space coordinates, and avoid floating point errors.

    :param atom: Atom to get key for
    :type atom: :class:`nanome.structure.Atom`
    :return: Position key tuple
    :rtype: (int, int, int)
    """
    return tuple(map(lambda x: int(50 * round(x, 4)), atom.position))


REDUCE_PATH = ""
REDUCE_DICT_PATH = os.path.join(BASE_DIR, "data/reduce_wwPDB_het_dict.txt")

if sys.platform == 'linux':
    REDUCE_PATH = os.path.join(BASE_DIR, 'bin/linux/reduce')
elif sys.platform == 'darwin':
    REDUCE_PATH = os.path.join(BASE_DIR, 'bin/darwin/reduce')
elif sys.platform == 'win32':
    REDUCE_PATH = os.path.join(BASE_DIR, 'bin/win32/reduce.exe')
    REDUCE_DICT_PATH = REDUCE_DICT_PATH.replace("\\", "/")

current_output = ""

async def call_Reduce(pdb_path, output_pdb, flip, histidines):

    def output_to_file(*args, **kwargs):
        global current_output
        msg = ' '.join(map(str, args))
        current_output += msg

    p = Process()
    p.executable_path = REDUCE_PATH
    p.args = ["-DROP_HYDROGENS_ON_ATOM_RECORDS", "-DROP_HYDROGENS_ON_OTHER_RECORDS", "-DB", REDUCE_DICT_PATH, pdb_path, "-H2OOCCcutoff0.0", "-H2OBcutoff99"]

    if flip:
        p.args = ["-FLIP"] + p.args
    if histidines:
        p.args = ["-HIS"] + p.args

    p.on_error = Logs.error
    p.on_output = output_to_file
    p.output_text = True

    Logs.debug("Starting Reduce with arguments:", p.args)

    # if error, notify user and return
    exit_code = await p.start()
    if exit_code < 0:
        Logs.error("Reduce returned an error", exit_code)
        return (-1, -1)
    
    #write to file
    for l in current_output.splitlines():
        if l.startswith("ATOM") or l.startswith("HETATM") or l.startswith("CONECT"):
            output_pdb.write((l + "\n").encode())
    output_pdb.close()
    
    hydrogenated = Complex.io.from_pdb(path=output_pdb.name)
    new_hydrogens = []

    id = 0
    with open(output_pdb.name) as f:
        lines = f.readlines()
        for line in lines:
            if len(line) > 84 and line.endswith('new\n'):
                new_hydrogens.append(id)
            id+=1

    return (hydrogenated, new_hydrogens)

def main():
    plugin = nanome.Plugin('Reduce hydrogenation', 'Hydrogenation plugin using Reduce', 'Hydrogens', False)
    plugin.set_plugin_class(ReducePlugin)
    plugin.run()


if __name__ == '__main__':
    main()
