"""Source-labelled AGS variants preserve complete boreholes and linked results."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boreholeai._merge import merge_results
from boreholeai._merge._ags import AgsMergeConflict, AgsSource, merge_ags_files
from boreholeai._batch import BatchResult, _job_workdir
from boreholeai.client import BoreholeAI

FIXTURES = json.loads((Path(__file__).parent / 'fixtures/ags-source-variants.json').read_text())


def parse(text):
    groups = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if row[0] == 'GROUP':
            current = groups.setdefault(row[1], {'rows': []})
        elif row[0] == 'HEADING':
            current['headings'] = row[1:]
        elif row[0] == 'DATA':
            current['rows'].append(dict(zip(current['headings'], row[1:])))
        else:
            current[row[0]] = dict(zip(current['headings'], row[1:]))
    return groups


def normalized(text):
    return {name: sorted(json.dumps(row, sort_keys=True) for row in group['rows'])
            for name, group in parse(text).items()}


class AgsSourceVariantTests(unittest.TestCase):
    def test_shared_source_variant_fixtures(self):
        for case in FIXTURES:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as folder:
                paths = []
                for index, content in enumerate(case['inputs']):
                    path = Path(folder) / f'{index}.ags'
                    path.write_text(content)
                    paths.append(path)
                sources = [AgsSource(**source) for source in case['sources']]
                mapping = []
                if 'error' in case:
                    with self.assertRaisesRegex(AgsMergeConflict, case['error']):
                        merge_ags_files(paths, sources=sources, location_mapping=mapping)
                    self.assertEqual(mapping, [])
                    continue
                output = merge_ags_files(paths, sources=sources, location_mapping=mapping)
                groups = parse(output)
                self.assertTrue(output.isascii())
                for group, count in case['counts'].items():
                    self.assertEqual(len(groups[group]['rows']), count, group)
                ids = {row['LOCA_ID'] for row in groups['LOCA']['rows']}
                self.assertEqual(len(ids), case['counts']['LOCA'])
                self.assertEqual(len(mapping), len(sources))
                self.assertTrue(all(row['merged_loca_id'] in ids for row in mapping))
                self.assertEqual([[row['source_file'], row['source_job_id']] for row in mapping],
                                 [[source.source_file, source.job_id] for source in sources])
                if 'ids' in case:
                    self.assertEqual(ids, set(case['ids']))
                if case.get('renamed'):
                    self.assertTrue(all(loca.startswith('BH1__') for loca in ids))
                    for row in groups['LOCA']['rows']:
                        self.assertIn('original LOCA_ID="BH1"', row['LOCA_REM'])
                        self.assertIn('physical identity not established', row['LOCA_REM'])
                    provenance = [source for row in groups['LOCA']['rows']
                                  for source in json.loads(row['LOCA_REM'].split('; sources=')[1])]
                    self.assertEqual(sorted(provenance), sorted([s.source_file, s.job_id] for s in sources))
                for group in groups.values():
                    for row in group['rows']:
                        if 'LOCA_ID' in row:
                            self.assertIn(row['LOCA_ID'], ids)
                headings = groups['LOCA']['headings']
                if 'LOCA_REM' in headings:
                    self.assertLess(headings.index('LOCA_REM'), headings.index('LOCA_FDEP'))
                    self.assertEqual(groups['LOCA']['TYPE']['LOCA_REM'], 'X')
                if 'proj' in case:
                    self.assertEqual(len(groups['PROJ']['rows']), 1)
                    for heading, value in case['proj'].items():
                        self.assertEqual(groups['PROJ']['rows'][0][heading], value, heading)
                    proj_headings = groups['PROJ']['headings']
                    if 'FILE_FSET' in proj_headings:
                        self.assertLess(proj_headings.index('PROJ_MEMO'), proj_headings.index('FILE_FSET'))
                if 'depths' in case:
                    self.assertEqual(sorted(row['LOCA_FDEP'] for row in groups['LOCA']['rows']), case['depths'])
                if 'results' in case:
                    self.assertEqual(sorted(row['RPLT_PLSI'] for row in groups['RPLT']['rows']), case['results'])
                sample_keys = ('LOCA_ID','SAMP_TOP','SAMP_REF','SAMP_TYPE','SAMP_ID')
                parents = {tuple(row[key] for key in sample_keys) for row in groups.get('SAMP', {}).get('rows', [])}
                for row in groups.get('RPLT', {}).get('rows', []):
                    self.assertIn(tuple(row[key] for key in sample_keys), parents)
                if case.get('links'):
                    tran = groups['TRAN']['rows'][0]
                    for row in groups['SAMP']['rows']:
                        links = row['SAMP_LINK'].split(tran['TRAN_RCON'])
                        for link in links:
                            self.assertEqual(link.split(tran['TRAN_DLIM']), ['LOCA', row['LOCA_ID']])
                reverse = merge_ags_files(reversed(paths), sources=reversed(sources))
                self.assertEqual(normalized(output), normalized(reverse))
                self.assertEqual([path.read_text() for path in paths], case['inputs'])

    def test_generated_identifiers_cannot_collide_with_existing_boreholes(self):
        case = next(case for case in FIXTURES if case['name'] == 'changed_test_only')
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f'{index}.ags' for index in range(3)]
            for path, content in zip(paths, case['inputs']):
                path.write_text(content)
            sources = [AgsSource(**source) for source in case['sources']]
            first = parse(merge_ags_files(paths[:2], sources=sources))
            reserved = first['LOCA']['rows'][0]['LOCA_ID']
            paths[2].write_text(case['inputs'][0].replace('BH1', reserved))
            sources.append(AgsSource('ExistingName.pdf', 'job-c'))
            merged = parse(merge_ags_files(paths, sources=sources))
            ids = [row['LOCA_ID'] for row in merged['LOCA']['rows']]
            self.assertEqual(len(set(ids)), 3)
            self.assertIn(reserved, ids)

    def test_merge_results_passes_source_filename_and_real_job_id(self):
        case = next(case for case in FIXTURES if case['name'] == 'changed_test_only')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dirs = [root/'Human readable cache A', root/'Human readable cache B']
            for directory, content in zip(dirs, case['inputs']):
                directory.mkdir()
                (directory/'Borehole_ags4.ags').write_text(content)
                (directory/'Borehole_data.json').write_text(json.dumps({
                    'test_data': {'Is50': [{'Hole_ID':'BH1', 'Is50':0.45}]},
                    'ags_export': {'reconciliation': [{'group':'RPLT', 'destination_key':['BH1','2.00','001']}]},
                }))
            labels = {directory: source['source_file'] for directory, source in zip(dirs, case['sources'])}
            jobs = {directory: source['job_id'] for directory, source in zip(dirs, case['sources'])}
            result = merge_results(dirs, root/'merged', dir_labels=labels, dir_job_ids=jobs)
            path = root/'merged/Borehole_ags4_merged.ags'
            self.assertIn(path.resolve(), result.files)
            self.assertFalse(any('No merged AGS' in warning for warning in result.warnings))
            groups = parse(path.read_text())
            provenance = [source for row in groups['LOCA']['rows']
                          for source in json.loads(row['LOCA_REM'].split('; sources=')[1])]
            self.assertEqual(sorted(provenance), sorted([[labels[d], jobs[d]] for d in dirs]))
            payload = json.loads((root/'merged/Borehole_data_merged.json').read_text())
            audit = payload['ags_export']
            self.assertEqual(audit['merge_status'], 'created')
            self.assertEqual(audit['reconciliation_scope'], 'source_ags')
            self.assertEqual(audit['merged_ags_file'], path.name)
            for row, source in zip(audit['reconciliation'], case['sources']):
                self.assertEqual(row['destination_key'], ['BH1','2.00','001'])
                self.assertEqual(row['destination_scope'], 'source_ags')
                self.assertEqual(row['source_document'], source['source_file'])
                self.assertEqual(row['source_job_id'], source['job_id'])
            self.assertEqual({row['merged_loca_id'] for row in audit['location_mapping']},
                             {row['LOCA_ID'] for row in groups['LOCA']['rows']})
            self.assertTrue(all(row['Hole_ID']=='BH1' for row in payload['test_data']['Is50']))

            # An incompatible rerun must not advertise stale successful mappings.
            (dirs[1]/'Borehole_ags4.ags').write_text(case['inputs'][1].replace('MPa', 'kPa'))
            merge_results(dirs, root/'merged', dir_labels=labels, dir_job_ids=jobs)
            audit = json.loads((root/'merged/Borehole_data_merged.json').read_text())['ags_export']
            self.assertEqual(audit['merge_status'], 'not_created')
            self.assertIsNone(audit['merged_ags_file'])
            self.assertEqual(audit['location_mapping'], [])
            self.assertFalse(path.exists())

    def test_batch_finalise_preserves_real_job_provenance(self):
        case = next(case for case in FIXTURES if case['name'] == 'changed_test_only')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jobs = {source['source_file']: source['job_id'] for source in case['sources']}
            batch = BatchResult(successes=list(jobs), job_ids=jobs, workdir=root/'cache')
            for source, content in zip(case['sources'], case['inputs']):
                directory = _job_workdir(batch.workdir, source['source_file'], source['job_id'])
                directory.mkdir(parents=True)
                (directory/'Borehole_ags4.ags').write_text(content)
            with patch('boreholeai.client._log'):
                BoreholeAI(api_key='offline-test')._finalise(
                    batch, [Path(name) for name in jobs], root/'merged', 0,
                    finalise_and_cleanup=False,
                )
            groups = parse((root/'merged/Borehole_ags4_merged.ags').read_text())
            provenance = [source for row in groups['LOCA']['rows']
                          for source in json.loads(row['LOCA_REM'].split('; sources=')[1])]
            self.assertEqual(sorted(provenance), sorted([list(item) for item in jobs.items()]))


if __name__ == '__main__':
    unittest.main()
