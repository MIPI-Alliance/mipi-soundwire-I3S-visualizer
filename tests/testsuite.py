#!/usr/bin/env python3

"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import unittest
import os
import subprocess

class CompareFramesTest(unittest.TestCase):

    # TODO: paths are not managed robustly. Currently
    # running only under tests/
  
    @classmethod
    def setUpClass(cls):
        # Set paths for configurations, test outputs and
        # reference (i.e. golden) outputs
        cur_dir = os.path.abspath(os.path.dirname(__file__))
        print(f'############## {cur_dir}')
        cls.cfg_dir = os.path.join(cur_dir, "test_cfgs")
        # TODO: the reference directory is temporarily called
        # tmp_refs/ to stress that the files there cannot be
        # considered actual references until they are validated
        # by someone
        cls.ref_dir = os.path.join(cur_dir, "tmp_refs")
        cls.out_dir = os.path.join(cur_dir, "test_outs")
        cls.script = os.path.normpath(os.path.join(cur_dir, '../swi3s_visualizer.py'))
        # Create the test output directory, stopping if it
        # already exists (alternatively we can remove it,
        # but I prefer to be on the safe side)
        print(f"Creating {cls.out_dir}")
        os.mkdir(cls.out_dir)
        # Find all the configuration files we want to test
        # (by default we will test all csv files in cfg_dir)
        cls.cfg_list = []
        for root, dirs, files in os.walk(cls.cfg_dir):
            for file in files:
                if file.endswith('.csv'):
                    cls.cfg_list.append(os.path.join(root, file))
        print(f'configs = {cls.cfg_list}')
      
    def config_test(self, cfg):
        # Infer test and reference output file names
        cfg_relpath = os.path.relpath(cfg, self.cfg_dir)
        print (f'Will run test for config {cfg_relpath}')
        (cfg_basename, ext) = os.path.splitext(cfg_relpath)
        json_relpath = cfg_basename + '.json'
        out_path = os.path.join(self.out_dir, json_relpath)
        ref_path = os.path.join(self.ref_dir, json_relpath)

        # Call visualizer
        cmd = f"python3 {self.script} -c {cfg} -o {out_path} -b"
        print (f'cmd = {cmd}')
        result = subprocess.run([cmd], shell=True, check=True)
        self.assertTrue(result.returncode == 0, f"swi3s_visualizer exited with code {result.returncode}")    
        # Compare output file with reference
        cmd = f"cmp {ref_path} {out_path}"
        print (f'cmd = {cmd}')
        result = subprocess.run([cmd], shell=True, check=True)
        self.assertTrue(result.returncode == 0, f"Compare failed")    
      
    def test_regression(self):
        for cfg in self.cfg_list:
            with self.subTest(cfg=cfg):
               self.config_test(cfg)


if __name__ == '__main__':
    unittest.main()
    
