# -*- coding: utf-8 -*-
"""
Created on Wed Apr  5 14:02:41 2023 by Guido Meijer
"""

import os
from os.path import join, split, isfile, isdir, dirname, realpath
import numpy as np
import pandas as pd
from datetime import datetime
import shutil
from glob import glob
from pathlib import Path
import json

from ibllib.ephys import ephysqc
from ibllib.ephys.spikes import ks2_to_alf, sync_spike_sorting
from ibllib.pipes.ephys_tasks import (EphysCompressNP1, EphysSyncPulses, EphysSyncRegisterRaw,
                                      EphysPulses)

import spikeinterface.extractors as se
import spikeinterface.preprocessing as spre
from spikeinterface.sorters import run_sorter, get_default_sorter_params


# Load in setting files
with open(join(dirname(realpath(__file__)), 'settings.json'), 'r') as openfile:
    settings_dict = json.load(openfile)
with open(join(dirname(realpath(__file__)), 'nidq.wiring.json'), 'r') as openfile:
    nidq_sync_dictionary = json.load(openfile)
with open(join(dirname(realpath(__file__)),
               f'{nidq_sync_dictionary["SYSTEM"]}.wiring.json'), 'r') as openfile:
    probe_sync_dictionary = json.load(openfile)
    
# Load in spike sorting parameters
if isfile(join(dirname(realpath(__file__)), f'{settings_dict["SPIKE_SORTER"]}_params.json')):
    with open(join(dirname(realpath(__file__)),
                   f'{settings_dict["SPIKE_SORTER"]}_params.json'), 'r') as openfile:
        sorter_params = json.load(openfile)
else:
    sorter_params = get_default_sorter_params(settings_dict['SPIKE_SORTER'])

# Initialize Matlab engine for bombcell package
if settings_dict['RUN_BOMBCELL']:
    import matlab.engine
    eng = matlab.engine.start_matlab()
    eng.addpath(r"{}".format(os.path.dirname(os.path.realpath(__file__))), nargout=0)
    eng.addpath(eng.genpath(settings_dict['BOMBCELL_PATH']))
    eng.addpath(settings_dict['MATLAB_NPY_PATH'])

# Search for spikesort_me.flag
print('Looking for spikesort_me.flag..')
for root, directory, files in os.walk(settings_dict['DATA_FOLDER']):
    if 'spikesort_me.flag' in files:
        session_path = Path(root)
        print(f'\nFound spikesort_me.flag in {root}')
        print(f'Starting pipeline at {datetime.now().strftime("%H:%M")}')
        
        # Restructure file and folders
        if 'probe00' not in os.listdir(join(root, 'raw_ephys_data')):
            if len(os.listdir(join(root, 'raw_ephys_data'))) == 0:
                print('No ephys data found')
                continue
            elif len(os.listdir(join(root, 'raw_ephys_data'))) > 1:
                print('More than one run found, not supported')
                continue
            orig_dir = os.listdir(join(root, 'raw_ephys_data'))[0]
            for i, this_dir in enumerate(os.listdir(join(root, 'raw_ephys_data', orig_dir))):
                shutil.move(join(root, 'raw_ephys_data', orig_dir, this_dir),
                            join(root, 'raw_ephys_data'))
            os.rmdir(join(root, 'raw_ephys_data', orig_dir))
            for i, this_path in enumerate(glob(join(root, 'raw_ephys_data', '*imec*'))):
                os.rename(this_path, join(root, 'raw_ephys_data', 'probe0' + this_path[-1]))
                
        # Create synchronization file
        nidq_file = next(session_path.joinpath('raw_ephys_data').glob('*.nidq.*bin'))
        with open(nidq_file.with_suffix('.wiring.json'), 'w') as fp:
            json.dump(nidq_sync_dictionary, fp, indent=1)
        
        for ap_file in session_path.joinpath('raw_ephys_data').rglob('*.ap.cbin'):
            with open(ap_file.with_suffix('.wiring.json'), 'w') as fp:
                json.dump(probe_sync_dictionary, fp, indent=1)
        
        # Create nidq sync file
        EphysSyncRegisterRaw(session_path=session_path, sync_collection='raw_ephys_data').run()
                
        probes = glob(join(root, 'raw_ephys_data', 'probe*'))
        for i, this_probe in enumerate(probes):
            
            if isdir(join(root, this_probe[-7:])):
                print('Probe already processed, moving on')
                continue
            
            # Create probe sync file
            task = EphysSyncPulses(session_path=session_path, sync='nidq', pname=this_probe[-7:],
                                   sync_ext='bin', sync_namespace='spikeglx',
                                   sync_collection='raw_ephys_data',
                                   device_collection='raw_ephys_data')
            task.run()
            task = EphysPulses(session_path=session_path, pname=this_probe[-7:],
                               sync_collection='raw_ephys_data',
                               device_collection='raw_ephys_data')
            task.run()
            
            # Compute raw ephys QC metrics
            if not isfile(join(this_probe, '_iblqc_ephysSpectralDensityAP.power.npy')):
                task = ephysqc.EphysQC('', session_path=session_path, use_alyx=False)
                task.probe_path = Path(this_probe)
                task.run()
            
            # Load in recording            
            rec = se.read_spikeglx(this_probe, stream_id=f'imec{split(this_probe)[-1][-1]}.ap')
                                    
            # Pre-process 
            rec = spre.highpass_filter(rec)
            rec = spre.phase_shift(rec)
            bad_channel_ids, all_channels = spre.detect_bad_channels(rec)
            rec = spre.interpolate_bad_channels(rec, bad_channel_ids)
            rec = spre.highpass_spatial_filter(rec)
                            
            # Run spike sorting
            try:
                print(f'Starting {split(this_probe)[-1]} spike sorting at {datetime.now().strftime("%H:%M")}')
                sort = run_sorter(settings_dict['SPIKE_SORTER'], rec,
                                  output_folder=os.path.join(
                                      this_probe, settings_dict['SPIKE_SORTER'] + settings_dict['IDENTIFIER']),
                                  verbose=True, docker_image=True, **sorter_params)
            except Exception as err:
                print(err)
                
                # Log error to disk
                logf = open(os.path.join(this_probe, 'error_log.txt'), 'w')
                logf.write(str(err))
                logf.close()
                
                # Continue with next recording
                continue
            
            # Get AP and meta data files
            orig_ap_file = glob(join(root, 'raw_ephys_data', this_probe[-7:], '*ap.bin'))
            meta_file = glob(join(root, 'raw_ephys_data', this_probe[-7:], '*ap.meta'))
            
            # Run Bombcell
            if settings_dict['RUN_BOMBCELL']:
                print('Running Bombcell')
                eng.run_bombcell(join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'sorter_output'),
                                 orig_ap_file[0],
                                 meta_file[0],  
                                 join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'bombcell_qc'),
                                 this_probe,
                                 nargout=0)
            
            # Export spike sorting to alf files
            if not isdir(join(root, this_probe[-7:])):
                os.mkdir(join(root, this_probe[-7:]))
            ks2_to_alf(Path(join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'sorter_output')),
                       Path(join(root, 'raw_ephys_data', this_probe[-7:])),
                       Path(join(root, this_probe[-7:])))
            
            # Add bombcell QC to alf folder
            if settings_dict['RUN_BOMBCELL']:
                shutil.copy(join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'sorter_output', 'cluster_bc_unitType.tsv'),
                            join(root, this_probe[-7:], 'cluster_bc_unitType.tsv'))
                bc_unittype = pd.read_csv(join(root, this_probe[-7:], 'cluster_bc_unitType.tsv'), sep='\t')
                np.save(join(root, this_probe[-7:], 'clusters.bcUnitType'), bc_unittype['bc_unitType'])
            
            # Synchronize spike sorting to nidq clock
            ap_file = glob(join(root, 'raw_ephys_data', this_probe[-7:], '*ap.cbin'))[0]
            sync_spike_sorting(Path(ap_file), Path(join(root, this_probe[-7:])))
            
            # Delete copied recording.dat file
            if isfile(join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'sorter_output', 'recording.dat')):
                os.remove(join(this_probe, settings_dict['SPIKE_SORTER']+settings_dict['IDENTIFIER'], 'sorter_output', 'recording.dat'))
                
            # Compress raw data
            if len(glob(join(root, 'raw_ephys_data', this_probe[-7:], '*ap.cbin'))) == 0:
                print('Compressing raw binary file')
                task = EphysCompressNP1(session_path=Path(root), pname=this_probe[-7:])
                task.run()
                
            # Delete original raw data
            if len(orig_ap_file) == 1:
                try:
                    os.remove(orig_ap_file[0])
                except:
                    print('Could not remove uncompressed ap bin file, delete manually')
                    continue
            
            print(f'Done! At {datetime.now().strftime("%H:%M")}')
        
        # Delete spikesort_me.flag
        os.remove(os.path.join(root, 'spikesort_me.flag'))
