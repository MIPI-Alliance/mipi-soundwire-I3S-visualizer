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

class CompareFramesTest(unittest.TestCase):
  
    @classmethod
    def setUpClass(cls):
        # Set paths for configurations, test outputs and
        # reference (i.e. golden) outputs
        cur_dir = os.path.dirname(__file__)
        cls.cfg_dir = os.path.join(cur_dir, "test_cfgs")
        cls.ref_dir = os.path.join(cur_dir, "test_refs")
        cls.out_dir = os.path.join(cur_dir, "test_out")
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
                    cls.cfg_list.append(file)
        print(f'configs = {cls.cfg_list}')
        # TODO: delete
        cls.num_list = range(0, 8)


#    def setUp(self):
#        # Set paths for configurations, test outputs and
#        # reference (i.e. golden) outputs
#        cur_dir = os.path.dirname(__file__)
#        self.cfg_dir = os.path.join(cur_dir, "test_cfgs")
#        self.ref_dir = os.path.join(cur_dir, "test_refs")
#        self.out_dir = os.path.join(cur_dir, "test_out")
#
#        # Stop if the test output directory exists
#        # (alternatively we can remove it, but I prefer to be
#        # on the safe side)
##        if (os.path.exists(self.out_dir)):
##            print(f"Error: {self.out_dir} already exists")
##            exit(1)
##        else:
#        os.mkdir(self.out_dir)

        # Find all the configuration files we want to test
        # (by default we will test all those in cfg_dir)
        

    def test_basic(self):
        self.assertEqual(1, 1)

    def test_failing(self):
        self.assertEqual(1, 2)

    def evenness_test(self, i):
         self.assertEqual(i % 2, 0)

    def test_multiple(self):
        for i in self.num_list:
            with self.subTest(i=i):
               self.evenness_test(i)


if __name__ == '__main__':
    unittest.main()
    
