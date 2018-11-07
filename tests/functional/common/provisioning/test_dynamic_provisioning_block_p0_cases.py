import time
from unittest import skip

from cnslibs.cns.cns_baseclass import CnsGlusterBlockBaseClass
from cnslibs.common.exceptions import ExecutionError
from cnslibs.common.openshift_ops import (
    get_gluster_pod_names_by_pvc_name,
    get_pod_name_from_dc,
    get_pv_name_from_pvc,
    get_pvc_status,
    oc_create_app_dc_with_io,
    oc_create_secret,
    oc_create_sc,
    oc_create_pvc,
    oc_delete,
    oc_get_custom_resource,
    oc_rsh,
    scale_dc_pod_amount_and_wait,
    verify_pvc_status_is_bound,
    wait_for_pod_be_ready,
    wait_for_resource_absence
    )
from cnslibs.common.heketi_ops import (
    heketi_blockvolume_delete,
    heketi_blockvolume_list
    )
from cnslibs.common.waiter import Waiter
from glusto.core import Glusto as g


class TestDynamicProvisioningBlockP0(CnsGlusterBlockBaseClass):
    '''
     Class that contain P0 dynamic provisioning test cases
     for block volume
    '''

    def setUp(self):
        super(TestDynamicProvisioningBlockP0, self).setUp()
        self.node = self.ocp_master_node[0]
        self.sc = self.cns_storage_class['storage_class2']

    def _create_storage_class(self, hacount=True, create_name_prefix=False,
                              reclaim_policy="Delete"):
        secret = self.cns_secret['secret2']

        # Create secret file
        self.secret_name = oc_create_secret(
            self.node, namespace=secret['namespace'],
            data_key=self.heketi_cli_key, secret_type=secret['type'])
        self.addCleanup(oc_delete, self.node, 'secret', self.secret_name)

        # create storage class
        kwargs = {
            "provisioner": "gluster.org/glusterblock",
            "resturl": self.sc['resturl'],
            "restuser": self.sc['restuser'],
            "restsecretnamespace": self.sc['restsecretnamespace'],
            "restsecretname": self.secret_name
        }
        if hacount:
            kwargs["hacount"] = self.sc['hacount']
        if create_name_prefix:
            kwargs["volumenameprefix"] = self.sc.get(
                    'volumenameprefix', 'autotest-blk')

        self.sc_name = oc_create_sc(
            self.node, reclaim_policy=reclaim_policy, **kwargs)
        self.addCleanup(oc_delete, self.node, 'sc', self.sc_name)

        return self.sc_name

    def _create_and_wait_for_pvcs(self, pvc_size=1,
                                  pvc_name_prefix='autotests-block-pvc',
                                  pvc_amount=1):
        # Create PVCs
        pvc_names = []
        for i in range(pvc_amount):
            pvc_name = oc_create_pvc(
                self.node, self.sc_name, pvc_name_prefix=pvc_name_prefix,
                pvc_size=pvc_size)
            pvc_names.append(pvc_name)
            self.addCleanup(
                wait_for_resource_absence, self.node, 'pvc', pvc_name)

        # Wait for PVCs to be in bound state
        try:
            for pvc_name in pvc_names:
                verify_pvc_status_is_bound(self.node, pvc_name)
        finally:
            reclaim_policy = oc_get_custom_resource(
                self.node, 'sc', ':.reclaimPolicy', self.sc_name)[0]

            for pvc_name in pvc_names:
                if reclaim_policy == 'Retain':
                    pv_name = get_pv_name_from_pvc(self.node, pvc_name)
                    self.addCleanup(oc_delete, self.node, 'pv', pv_name,
                                    raise_on_absence=False)
                    custom = (r':.metadata.annotations."gluster\.kubernetes'
                              r'\.io\/heketi\-volume\-id"')
                    vol_id = oc_get_custom_resource(
                        self.node, 'pv', custom, pv_name)[0]
                    self.addCleanup(heketi_blockvolume_delete,
                                    self.heketi_client_node,
                                    self.heketi_server_url, vol_id)
                self.addCleanup(oc_delete, self.node, 'pvc', pvc_name,
                                raise_on_absence=False)

        return pvc_names

    def _create_and_wait_for_pvc(self, pvc_size=1,
                                 pvc_name_prefix='autotests-block-pvc'):
        self.pvc_name = self._create_and_wait_for_pvcs(
            pvc_size=pvc_size, pvc_name_prefix=pvc_name_prefix)[0]
        return self.pvc_name

    def _create_dc_with_pvc(self, hacount=True, create_name_prefix=False):
        # Create storage class and secret objects
        self._create_storage_class(
                hacount, create_name_prefix=create_name_prefix)

        # Create PVC
        pvc_name = self._create_and_wait_for_pvc()

        # Create DC with POD and attached PVC to it
        dc_name = oc_create_app_dc_with_io(self.node, pvc_name)
        self.addCleanup(oc_delete, self.node, 'dc', dc_name)
        self.addCleanup(scale_dc_pod_amount_and_wait, self.node, dc_name, 0)
        pod_name = get_pod_name_from_dc(self.node, dc_name)
        wait_for_pod_be_ready(self.node, pod_name)

        return dc_name, pod_name, pvc_name

    def dynamic_provisioning_glusterblock(
            self, hacount=True, create_name_prefix=False):
        datafile_path = '/mnt/fake_file_for_%s' % self.id()

        # Create DC with attached PVC
        dc_name, pod_name, pvc_name = self._create_dc_with_pvc(
                hacount, create_name_prefix=create_name_prefix)

        # Check that we can write data
        for cmd in ("dd if=/dev/urandom of=%s bs=1K count=100",
                    "ls -lrt %s",
                    "rm -rf %s"):
            cmd = cmd % datafile_path
            ret, out, err = oc_rsh(self.node, pod_name, cmd)
            self.assertEqual(
                ret, 0,
                "Failed to execute '%s' command on '%s'." % (cmd, self.node))

    def test_dynamic_provisioning_glusterblock_hacount_true(self):
        """ CNS-435 dynamic provisioning glusterblock """
        self.dynamic_provisioning_glusterblock()

    def test_dynamic_provisioning_glusterblock_hacount_false(self):
        """ CNS-716 storage-class mandatory parameters for block """
        self.dynamic_provisioning_glusterblock(hacount=False)

    def test_dynamic_provisioning_glusterblock_heketipod_failure(self):
        datafile_path = '/mnt/fake_file_for_%s' % self.id()

        # Create DC with attached PVC
        app_1_dc_name, app_1_pod_name, app_1_pvc_name = (
            self._create_dc_with_pvc())

        # Write test data
        write_data_cmd = (
            "dd if=/dev/urandom of=%s bs=1K count=100" % datafile_path)
        ret, out, err = oc_rsh(self.node, app_1_pod_name, write_data_cmd)
        self.assertEqual(
            ret, 0,
            "Failed to execute command %s on %s" % (write_data_cmd, self.node))

        # Remove Heketi pod
        heketi_down_cmd = "oc scale --replicas=0 dc/%s --namespace %s" % (
            self.heketi_dc_name, self.cns_project_name)
        heketi_up_cmd = "oc scale --replicas=1 dc/%s --namespace %s" % (
            self.heketi_dc_name, self.cns_project_name)
        self.addCleanup(self.cmd_run, heketi_up_cmd)
        heketi_pod_name = get_pod_name_from_dc(
            self.node, self.heketi_dc_name, timeout=10, wait_step=3)
        self.cmd_run(heketi_down_cmd)
        wait_for_resource_absence(self.node, 'pod', heketi_pod_name)

        # Create second PVC
        app_2_pvc_name = oc_create_pvc(
            self.node, self.sc_name, pvc_name_prefix='autotests-block-pvc',
            pvc_size=1)
        self.addCleanup(
            wait_for_resource_absence, self.node, 'pvc', app_2_pvc_name)
        self.addCleanup(oc_delete, self.node, 'pvc', app_2_pvc_name)

        # Check status of the second PVC after small pause
        time.sleep(2)
        ret, status = get_pvc_status(self.node, app_2_pvc_name)
        self.assertTrue(ret, "Failed to get pvc status of %s" % app_2_pvc_name)
        self.assertEqual(
            status, "Pending",
            "PVC status of %s is not in Pending state" % app_2_pvc_name)

        # Create second app POD
        app_2_dc_name = oc_create_app_dc_with_io(self.node, app_2_pvc_name)
        self.addCleanup(oc_delete, self.node, 'dc', app_2_dc_name)
        self.addCleanup(
            scale_dc_pod_amount_and_wait, self.node, app_2_dc_name, 0)
        app_2_pod_name = get_pod_name_from_dc(self.node, app_2_dc_name)

        # Bring Heketi pod back
        self.cmd_run(heketi_up_cmd)

        # Wait for Heketi POD be up and running
        new_heketi_pod_name = get_pod_name_from_dc(
            self.node, self.heketi_dc_name, timeout=10, wait_step=2)
        wait_for_pod_be_ready(
            self.node, new_heketi_pod_name, wait_step=5, timeout=120)

        # Wait for second PVC and app POD be ready
        verify_pvc_status_is_bound(self.node, app_2_pvc_name)
        wait_for_pod_be_ready(
            self.node, app_2_pod_name, timeout=150, wait_step=3)

        # Verify that we are able to write data
        ret, out, err = oc_rsh(self.node, app_2_pod_name, write_data_cmd)
        self.assertEqual(
            ret, 0,
            "Failed to execute command %s on %s" % (write_data_cmd, self.node))

    @skip("Blocked by BZ-1632873")
    def test_dynamic_provisioning_glusterblock_glusterpod_failure(self):
        datafile_path = '/mnt/fake_file_for_%s' % self.id()

        # Create DC with attached PVC
        dc_name, pod_name, pvc_name = self._create_dc_with_pvc()

        # Run IO in background
        io_cmd = "oc rsh %s dd if=/dev/urandom of=%s bs=1000K count=900" % (
            pod_name, datafile_path)
        async_io = g.run_async(self.node, io_cmd, "root")

        # Pick up one of the hosts which stores PV brick (4+ nodes case)
        gluster_pod_data = get_gluster_pod_names_by_pvc_name(
            self.node, pvc_name)[0]

        # Delete glusterfs POD from chosen host and wait for spawn of new one
        oc_delete(self.node, 'pod', gluster_pod_data["pod_name"])
        cmd = ("oc get pods -o wide | grep glusterfs | grep %s | "
               "grep -v Terminating | awk '{print $1}'") % (
                   gluster_pod_data["host_name"])
        for w in Waiter(600, 30):
            out = self.cmd_run(cmd)
            new_gluster_pod_name = out.strip().split("\n")[0].strip()
            if not new_gluster_pod_name:
                continue
            else:
                break
        if w.expired:
            error_msg = "exceeded timeout, new gluster pod not created"
            g.log.error(error_msg)
            raise ExecutionError(error_msg)
        new_gluster_pod_name = out.strip().split("\n")[0].strip()
        g.log.info("new gluster pod name is %s" % new_gluster_pod_name)
        wait_for_pod_be_ready(self.node, new_gluster_pod_name)

        # Check that async IO was not interrupted
        ret, out, err = async_io.async_communicate()
        self.assertEqual(ret, 0, "IO %s failed on %s" % (io_cmd, self.node))

    def test_glusterblock_logs_presence_verification(self):
        # Verify presence of glusterblock provisioner POD and its status
        gb_prov_cmd = ("oc get pods --all-namespaces "
                       "-l glusterfs=block-cns-provisioner-pod "
                       "-o=custom-columns=:.metadata.name,:.status.phase")
        ret, out, err = g.run(self.ocp_client[0], gb_prov_cmd, "root")

        self.assertEqual(ret, 0, "Failed to get Glusterblock provisioner POD.")
        gb_prov_name, gb_prov_status = out.split()
        self.assertEqual(gb_prov_status, 'Running')

        # Create storage class and secret objects
        self._create_storage_class()

        # Create PVC
        self._create_and_wait_for_pvc()

        # Get list of Gluster PODs
        g_pod_list_cmd = (
            "oc get pods --all-namespaces -l glusterfs-node=pod "
            "-o=custom-columns=:.metadata.name,:.metadata.namespace")
        ret, out, err = g.run(self.ocp_client[0], g_pod_list_cmd, "root")

        self.assertEqual(ret, 0, "Failed to get list of Gluster PODs.")
        g_pod_data_list = out.split()
        g_pods_namespace = g_pod_data_list[1]
        g_pods = [pod for pod in out.split()[::2]]
        logs = ("gluster-block-configshell", "gluster-blockd")

        # Verify presence and not emptiness of logs on Gluster PODs
        self.assertGreater(len(g_pods), 0, "We expect some PODs:\n %s" % out)
        for g_pod in g_pods:
            for log in logs:
                cmd = (
                    "oc exec -n %s %s -- "
                    "tail -n 5 /var/log/glusterfs/gluster-block/%s.log" % (
                        g_pods_namespace, g_pod, log))
                ret, out, err = g.run(self.ocp_client[0], cmd, "root")

                self.assertFalse(err, "Error output is not empty: \n%s" % err)
                self.assertEqual(ret, 0, "Failed to exec '%s' command." % cmd)
                self.assertTrue(out, "Command '%s' output is empty." % cmd)

    def test_dynamic_provisioning_glusterblock_heketidown_pvc_delete(self):
        """ Delete PVC's when heketi is down CNS-439 """

        # Create storage class and secret objects
        self._create_storage_class()

        self.pvc_name_list = self._create_and_wait_for_pvcs(
            1, 'pvc-heketi-down', 3)

        # remove heketi-pod
        scale_dc_pod_amount_and_wait(self.ocp_client[0],
                                     self.heketi_dc_name,
                                     0,
                                     self.cns_project_name)
        try:
            # delete pvc
            for pvc in self.pvc_name_list:
                oc_delete(self.ocp_client[0], 'pvc', pvc)
            for pvc in self.pvc_name_list:
                with self.assertRaises(ExecutionError):
                    wait_for_resource_absence(
                       self.ocp_client[0], 'pvc', pvc,
                       interval=3, timeout=30)
        finally:
            # bring back heketi-pod
            scale_dc_pod_amount_and_wait(self.ocp_client[0],
                                         self.heketi_dc_name,
                                         1,
                                         self.cns_project_name)

        # verify PVC's are deleted
        for pvc in self.pvc_name_list:
            wait_for_resource_absence(self.ocp_client[0], 'pvc',
                                      pvc,
                                      interval=1, timeout=120)

        # create a new PVC
        self._create_and_wait_for_pvc()

    def test_recreate_app_pod_with_attached_block_pv(self):
        """Test Case CNS-1392"""
        datafile_path = '/mnt/temporary_test_file'

        # Create DC with POD and attached PVC to it
        dc_name, pod_name, pvc_name = self._create_dc_with_pvc()

        # Write data
        write_cmd = "oc exec %s -- dd if=/dev/urandom of=%s bs=4k count=10000"
        self.cmd_run(write_cmd % (pod_name, datafile_path))

        # Recreate app POD
        scale_dc_pod_amount_and_wait(self.node, dc_name, 0)
        scale_dc_pod_amount_and_wait(self.node, dc_name, 1)
        new_pod_name = get_pod_name_from_dc(self.node, dc_name)

        # Check presence of already written file
        check_existing_file_cmd = (
            "oc exec %s -- ls %s" % (new_pod_name, datafile_path))
        out = self.cmd_run(check_existing_file_cmd)
        self.assertIn(datafile_path, out)

        # Perform I/O on the new POD
        self.cmd_run(write_cmd % (new_pod_name, datafile_path))

    def test_volname_prefix_glusterblock(self):
        # CNS-926 - custom_volname_prefix_blockvol

        self.dynamic_provisioning_glusterblock(create_name_prefix=True)

        pv_name = get_pv_name_from_pvc(self.node, self.pvc_name)
        vol_name = oc_get_custom_resource(
                self.node, 'pv',
                ':.metadata.annotations.glusterBlockShare', pv_name)[0]

        block_vol_list = heketi_blockvolume_list(
                self.heketi_client_node, self.heketi_server_url)

        self.assertIn(vol_name, block_vol_list)

        self.assertTrue(vol_name.startswith(
            self.sc.get('volumenameprefix', 'autotest-blk')))

    def test_dynamic_provisioning_glusterblock_reclaim_policy_retain(self):
        # CNS-1391 - Retain policy - gluster-block - delete pvc

        self._create_storage_class(reclaim_policy='Retain')
        self._create_and_wait_for_pvc()

        dc_name = oc_create_app_dc_with_io(self.node, self.pvc_name)

        try:
            pod_name = get_pod_name_from_dc(self.node, dc_name)
            wait_for_pod_be_ready(self.node, pod_name)
        finally:
            scale_dc_pod_amount_and_wait(self.node, dc_name, pod_amount=0)
            oc_delete(self.node, 'dc', dc_name)

        # get the name of volume
        pv_name = get_pv_name_from_pvc(self.node, self.pvc_name)

        custom = [r':.metadata.annotations."gluster\.org\/volume\-id"',
                  r':.spec.persistentVolumeReclaimPolicy']
        vol_id, reclaim_policy = oc_get_custom_resource(
            self.node, 'pv', custom, pv_name)

        # checking the retainPolicy of pvc
        self.assertEqual(reclaim_policy, 'Retain')

        # delete the pvc
        oc_delete(self.node, 'pvc', self.pvc_name)

        # check if pv is also deleted or not
        with self.assertRaises(ExecutionError):
            wait_for_resource_absence(
                self.node, 'pvc', self.pvc_name, interval=3, timeout=30)

        # getting the blockvol list
        blocklist = heketi_blockvolume_list(self.heketi_client_node,
                                            self.heketi_server_url)
        self.assertIn(vol_id, blocklist)

        heketi_blockvolume_delete(self.heketi_client_node,
                                  self.heketi_server_url, vol_id)
        blocklist = heketi_blockvolume_list(self.heketi_client_node,
                                            self.heketi_server_url)
        self.assertNotIn(vol_id, blocklist)
        oc_delete(self.node, 'pv', pv_name)
        wait_for_resource_absence(self.node, 'pv', pv_name)
