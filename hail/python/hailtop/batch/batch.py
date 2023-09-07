import os
import warnings
import re
from typing import Callable, Optional, Dict, Union, List, Any, Set, Literal, overload
from io import BytesIO
import dill

from hailtop.utils import secret_alnum_string, url_scheme, async_to_blocking
from hailtop.aiotools import AsyncFS
from hailtop.aiocloud.aioazure.fs import AzureAsyncFS
from hailtop.aiotools.router_fs import RouterAsyncFS
import hailtop.batch_client.client as _bc
from hailtop.config import ConfigVariable, configuration_of

from . import backend as _backend, job, resource as _resource  # pylint: disable=cyclic-import
from .exceptions import BatchException

class Batch:
    """Object representing the distributed acyclic graph (DAG) of jobs to run.

    Examples
    --------
    Create a batch object:

    >>> p = Batch()

    Create a new job that prints "hello":

    >>> t = p.new_job()
    >>> t.command(f'echo "hello" ')

    Execute the DAG:

    >>> p.run()

    Notes
    -----

    The methods :meth:`.Batch.read_input` and :meth:`.Batch.read_input_group`
    are for adding input files to a batch. An input file is a file that already
    exists before executing a batch and is not present in the docker container
    the job is being run in.

    Files generated by executing a job are temporary files and must be written
    to a permanent location using the method :meth:`.Batch.write_output`.

    Parameters
    ----------
    name:
        Name of the batch.
    backend:
        Backend used to execute the jobs. If no backend is specified, a backend
        will be created by first looking at the environment variable HAIL_BATCH_BACKEND,
        then the hailctl config variable batch/backend. These configurations, if set,
        can be either `local` or `service`, and will result in the use of a
        :class:`.LocalBackend` and :class:`.ServiceBackend` respectively. If no
        argument is given and no configurations are set, the default is
        :class:`.LocalBackend`.
    attributes:
        Key-value pairs of additional attributes. 'name' is not a valid keyword.
        Use the name argument instead.
    requester_pays_project:
        The name of the Google project to be billed when accessing requester pays buckets.
    default_image:
        Default docker image to use for Bash jobs. This must be the full name of the
        image including any repository prefix and tags if desired (default tag is `latest`).
    default_memory:
        Memory setting to use by default if not specified by a job. Only
        applicable if a docker image is specified for the :class:`.LocalBackend`
        or the :class:`.ServiceBackend`. See :meth:`.Job.memory`.
    default_cpu:
        CPU setting to use by default if not specified by a job. Only
        applicable if a docker image is specified for the :class:`.LocalBackend`
        or the :class:`.ServiceBackend`. See :meth:`.Job.cpu`.
    default_storage:
        Storage setting to use by default if not specified by a job. Only
        applicable for the :class:`.ServiceBackend`. See :meth:`.Job.storage`.
    default_timeout:
        Maximum time in seconds for a job to run before being killed. Only
        applicable for the :class:`.ServiceBackend`. If `None`, there is no
        timeout.
    default_python_image:
        Default image to use for all Python jobs. This must be the full name of the image including
        any repository prefix and tags if desired (default tag is `latest`).  The image must have
        the `dill` Python package installed and have the same version of Python installed that is
        currently running. If `None`, a compatible Python image with `dill` pre-installed will
        automatically be used if the current Python version is 3.9, or 3.10.
    default_spot:
        If unspecified or ``True``, jobs will run by default on spot instances. If ``False``, jobs
        will run by default on non-spot instances. Each job can override this setting with
        :meth:`.Job.spot`.
    project:
        DEPRECATED: please specify `google_project` on the ServiceBackend instead. If specified,
        the project to use when authenticating with Google Storage. Google Storage is used to
        transfer serialized values between this computer and the cloud machines that execute Python
        jobs.
    cancel_after_n_failures:
        Automatically cancel the batch after N failures have occurred. The default
        behavior is there is no limit on the number of failures. Only
        applicable for the :class:`.ServiceBackend`. Must be greater than 0.

    """

    _counter = 0
    _uid_prefix = "__BATCH__"
    _regex_pattern = r"(?P<BATCH>{}\d+)".format(_uid_prefix)  # pylint: disable=consider-using-f-string

    @classmethod
    def _get_uid(cls):
        uid = cls._uid_prefix + str(cls._counter)
        cls._counter += 1
        return uid

    @staticmethod
    def from_batch_id(batch_id: int, *args, **kwargs):
        """
        Create a Batch from an existing batch id.

        Notes
        -----
        Can only be used with the :class:`.ServiceBackend`.

        Examples
        --------

        Create a batch object from an existing batch id:

        >>> b = Batch.from_batch_id(1)  # doctest: +SKIP

        Parameters
        ----------
        batch_id:
            ID of an existing Batch

        Returns
        -------
        A Batch object that can append jobs to an existing batch.
        """
        b = Batch(*args, **kwargs)
        assert isinstance(b._backend, _backend.ServiceBackend)
        b._batch_handle = b._backend._batch_client.get_batch(batch_id)
        return b

    def __init__(self,
                 name: Optional[str] = None,
                 backend: Optional[_backend.Backend] = None,
                 attributes: Optional[Dict[str, str]] = None,
                 requester_pays_project: Optional[str] = None,
                 default_image: Optional[str] = None,
                 default_memory: Optional[Union[int, str]] = None,
                 default_cpu: Optional[Union[float, int, str]] = None,
                 default_storage: Optional[Union[int, str]] = None,
                 default_timeout: Optional[Union[float, int]] = None,
                 default_shell: Optional[str] = None,
                 default_python_image: Optional[str] = None,
                 default_spot: Optional[bool] = None,
                 project: Optional[str] = None,
                 cancel_after_n_failures: Optional[int] = None):
        self._jobs: List[job.Job] = []
        self._resource_map: Dict[str, _resource.Resource] = {}
        self._allocated_files: Set[str] = set()
        self._input_resources: Set[_resource.InputResourceFile] = set()
        self._uid = Batch._get_uid()
        self._job_tokens: Set[str] = set()

        if backend:
            self._backend = backend
        else:
            backend_config = configuration_of(ConfigVariable.BATCH_BACKEND, None, 'local')
            if backend_config == 'service':
                self._backend = _backend.ServiceBackend()
            else:
                assert backend_config == 'local'
                self._backend = _backend.LocalBackend()

        self.name = name

        if attributes is None:
            attributes = {}
        if 'name' in attributes:
            raise BatchException("'name' is not a valid attribute. Use the name argument instead.")
        self.attributes = attributes

        self.requester_pays_project = requester_pays_project

        self._default_image = default_image
        self._default_memory = default_memory
        self._default_cpu = default_cpu
        self._default_storage = default_storage
        self._default_timeout = default_timeout
        self._default_shell = default_shell
        self._default_python_image = default_python_image
        self._default_spot = default_spot

        if project is not None:
            warnings.warn(
                'The project argument to Batch is deprecated, please instead use the google_project argument to '
                'ServiceBackend. Use of this argument may trigger warnings from aiohttp about unclosed objects.')
        self._DEPRECATED_project = project
        self._DEPRECATED_fs: Optional[RouterAsyncFS] = None

        self._cancel_after_n_failures = cancel_after_n_failures

        self._python_function_defs: Dict[int, Callable] = {}
        self._python_function_files: Dict[int, _resource.InputResourceFile] = {}

        self._batch_handle: Optional[_bc.Batch] = None

    @property
    def _unsubmitted_jobs(self):
        return [j for j in self._jobs if not j._submitted]

    @property
    def _submitted_jobs(self):
        return [j for j in self._jobs if j._submitted]

    def _register_python_function(self, function: Callable) -> int:
        function_id = id(function)
        self._python_function_defs[function_id] = function
        return function_id

    async def _serialize_python_to_input_file(
        self, path: str, subdir: str, file_id: int, code: Any, dry_run: bool = False
    ) -> _resource.InputResourceFile:
        pipe = BytesIO()
        dill.dump(code, pipe, recurse=True)
        pipe.seek(0)

        code_path = f"{path}/{subdir}/code{file_id}.p"

        if not dry_run:
            await self._fs.makedirs(os.path.dirname(code_path), exist_ok=True)
            await self._fs.write(code_path, pipe.getvalue())

        code_input_file = self.read_input(code_path)

        return code_input_file

    async def _serialize_python_functions_to_input_files(
        self, path: str, dry_run: bool = False
    ) -> None:
        for function_id, function in self._python_function_defs.items():
            file = await self._serialize_python_to_input_file(
                path, "functions", function_id, function, dry_run
            )
            self._python_function_files[function_id] = file

    def _unique_job_token(self, n=5):
        token = secret_alnum_string(n)
        while token in self._job_tokens:
            token = secret_alnum_string(n)
        return token

    @property
    def _fs(self) -> AsyncFS:
        if self._DEPRECATED_project is not None:
            if self._DEPRECATED_fs is None:
                gcs_kwargs = {'gcs_requester_pays_configuration': self._DEPRECATED_project}
                self._DEPRECATED_fs = RouterAsyncFS(gcs_kwargs=gcs_kwargs)
            return self._DEPRECATED_fs
        return self._backend._fs

    def new_job(self,
                name: Optional[str] = None,
                attributes: Optional[Dict[str, str]] = None,
                shell: Optional[str] = None) -> job.BashJob:
        """
        Alias for :meth:`.Batch.new_bash_job`
        """

        return self.new_bash_job(name, attributes, shell)

    def new_bash_job(self,
                     name: Optional[str] = None,
                     attributes: Optional[Dict[str, str]] = None,
                     shell: Optional[str] = None) -> job.BashJob:
        """
        Initialize a :class:`.BashJob` object with default memory, storage,
        image, and CPU settings (defined in :class:`.Batch`) upon batch creation.

        Examples
        --------
        Create and execute a batch `b` with one job `j` that prints "hello world":

        >>> b = Batch()
        >>> j = b.new_bash_job(name='hello', attributes={'language': 'english'})
        >>> j.command('echo "hello world"')
        >>> b.run()

        Parameters
        ----------
        name:
            Name of the job.
        attributes:
            Key-value pairs of additional attributes. 'name' is not a valid keyword.
            Use the name argument instead.
        """

        if attributes is None:
            attributes = {}

        if shell is None:
            shell = self._default_shell

        token = self._unique_job_token()
        j = job.BashJob(batch=self, token=token, name=name, attributes=attributes, shell=shell)

        if self._default_image is not None:
            j.image(self._default_image)
        if self._default_memory is not None:
            j.memory(self._default_memory)
        if self._default_cpu is not None:
            j.cpu(self._default_cpu)
        if self._default_storage is not None:
            j.storage(self._default_storage)
        if self._default_timeout is not None:
            j.timeout(self._default_timeout)
        if self._default_spot is not None:
            j.spot(self._default_spot)

        if isinstance(self._backend, _backend.ServiceBackend):
            j.regions(self._backend.regions)

        self._jobs.append(j)
        return j

    def new_python_job(self,
                       name: Optional[str] = None,
                       attributes: Optional[Dict[str, str]] = None) -> job.PythonJob:
        """
        Initialize a new :class:`.PythonJob` object with default
        Python image, memory, storage, and CPU settings (defined in :class:`.Batch`)
        upon batch creation.

        Examples
        --------
        Create and execute a batch `b` with one job `j` that prints "hello alice":

        .. code-block:: python

            b = Batch(default_python_image='hailgenetics/python-dill:3.9-slim')

            def hello(name):
                return f'hello {name}'

            j = b.new_python_job()
            output = j.call(hello, 'alice')

            # Write out the str representation of result to a file

            b.write_output(output.as_str(), 'hello.txt')

            b.run()

        Notes
        -----

        The image to use for Python jobs can be specified by `default_python_image`
        when constructing a :class:`.Batch`. The image specified must have the `dill`
        package installed. If ``default_python_image`` is not specified, then a Docker
        image will automatically be created for you with the base image
        `hailgenetics/python-dill:[major_version].[minor_version]-slim` and the Python
        packages specified by ``python_requirements`` will be installed. The default name
        of the image is `batch-python` with a random string for the tag unless ``python_build_image_name``
        is specified. If the :class:`.ServiceBackend` is the backend, the locally built
        image will be pushed to the repository specified by ``image_repository``.

        Parameters
        ----------
        name:
            Name of the job.
        attributes:
            Key-value pairs of additional attributes. 'name' is not a valid keyword.
            Use the name argument instead.
        """
        if attributes is None:
            attributes = {}

        token = self._unique_job_token()
        j = job.PythonJob(batch=self, token=token, name=name, attributes=attributes)

        if self._default_python_image is not None:
            j.image(self._default_python_image)
        if self._default_memory is not None:
            j.memory(self._default_memory)
        if self._default_cpu is not None:
            j.cpu(self._default_cpu)
        if self._default_storage is not None:
            j.storage(self._default_storage)
        if self._default_timeout is not None:
            j.timeout(self._default_timeout)
        if self._default_spot is not None:
            j.spot(self._default_spot)

        if isinstance(self._backend, _backend.ServiceBackend):
            j.regions(self._backend.regions)

        self._jobs.append(j)
        return j

    def _new_job_resource_file(self, source, value=None):
        if value is None:
            value = secret_alnum_string(5)
        jrf = _resource.JobResourceFile(value, source)
        self._resource_map[jrf._uid] = jrf  # pylint: disable=no-member
        return jrf

    def _new_input_resource_file(self, input_path, root=None):
        self._backend.validate_file_scheme(input_path)

        # Take care not to include an Azure SAS token query string in the local name.
        if AzureAsyncFS.valid_url(input_path):
            file_name, _ = AzureAsyncFS.get_name_parts(input_path)
        else:
            file_name = input_path

        root = root or secret_alnum_string(5)
        irf = _resource.InputResourceFile(f'{root}/{os.path.basename(file_name.rstrip("/"))}')
        irf._add_input_path(input_path)
        self._resource_map[irf._uid] = irf  # pylint: disable=no-member
        self._input_resources.add(irf)
        return irf

    def _new_resource_group(self, source, mappings, root=None):
        assert isinstance(mappings, dict)
        if root is None:
            root = secret_alnum_string(5)
        d = {}
        new_resource_map = {}
        for name, code in mappings.items():
            if not isinstance(code, str):
                raise BatchException(f"value for name '{name}' is not a string. Found '{type(code)}' instead.")
            r = self._new_job_resource_file(source=source, value=eval(f'f"""{code}"""'))  # pylint: disable=W0123
            d[name] = r
            new_resource_map[r._uid] = r  # pylint: disable=no-member

        self._resource_map.update(new_resource_map)
        rg = _resource.ResourceGroup(source, root, **d)
        self._resource_map.update({rg._uid: rg})
        return rg

    def _new_python_result(self, source, value=None) -> _resource.PythonResult:
        if value is None:
            value = secret_alnum_string(5)
        jrf = _resource.PythonResult(value, source)
        self._resource_map[jrf._uid] = jrf  # pylint: disable=no-member
        return jrf

    def read_input(self, path: str) -> _resource.InputResourceFile:
        """
        Create a new input resource file object representing a single file.

        .. warning::

            To avoid expensive egress charges, input files should be located in buckets
            that are multi-regional in the United States because Batch runs jobs in any
            US region.

        Examples
        --------

        Read the file `hello.txt`:

        >>> b = Batch()
        >>> input = b.read_input('data/hello.txt')
        >>> j = b.new_job()
        >>> j.command(f'cat {input}')
        >>> b.run()

        Parameters
        ----------
        path: :obj:`str`
            File path to read.
        """

        irf = self._new_input_resource_file(path)
        return irf

    def read_input_group(self, **kwargs: str) -> _resource.ResourceGroup:
        """Create a new resource group representing a mapping of identifier to
        input resource files.

        .. warning::

            To avoid expensive egress charges, input files should be located in buckets
            that are multi-regional in the United States because Batch runs jobs in any
            US region.

        Examples
        --------

        Read a binary PLINK file:

        >>> b = Batch()
        >>> bfile = b.read_input_group(bed="data/example.bed",
        ...                            bim="data/example.bim",
        ...                            fam="data/example.fam")
        >>> j = b.new_job()
        >>> j.command(f"plink --bfile {bfile} --geno --make-bed --out {j.geno}")
        >>> j.command(f"wc -l {bfile.fam}")
        >>> j.command(f"wc -l {bfile.bim}")
        >>> b.run() # doctest: +SKIP

        Read a FASTA file and it's index (file extensions matter!):

        >>> fasta = b.read_input_group(**{'fasta': 'data/example.fasta',
        ...                               'fasta.idx': 'data/example.fasta.idx'})

        Create a resource group where the identifiers don't match the file extensions:

        >>> rg = b.read_input_group(foo='data/foo.txt',
        ...                         bar='data/bar.txt')

        `rg.foo` and `rg.bar` will not have the `.txt` file extension and
        instead will be `{root}.foo` and `{root}.bar` where `{root}` is a random
        identifier.

        Notes
        -----
        The identifier is used to refer to a specific resource file. For example,
        given the resource group `rg`, you can use the attribute notation
        `rg.identifier` or the get item notation `rg[identifier]`.

        The file extensions for each file are derived from the identifier.  This
        is equivalent to `"{root}.identifier"` from
        :meth:`.BashJob.declare_resource_group`. We are planning on adding
        flexibility to incorporate more complicated extensions in the future
        such as `.vcf.bgz`.  For now, use :meth:`.JobResourceFile.add_extension`
        to add an extension to a resource file.

        Parameters
        ----------
        kwargs:
            Key word arguments where the name/key is the identifier and the value
            is the file path.
        """

        root = secret_alnum_string(5)
        new_resources = {name: self._new_input_resource_file(file, root) for name, file in kwargs.items()}
        rg = _resource.ResourceGroup(None, root, **new_resources)
        self._resource_map.update({rg._uid: rg})
        return rg

    def write_output(self, resource: _resource.Resource, dest: str):
        """
        Write resource file or resource file group to an output destination.

        Examples
        --------

        Write a single job intermediate to a local file:

        >>> b = Batch()
        >>> j = b.new_job()
        >>> j.command(f'echo "hello" > {j.ofile}')
        >>> b.write_output(j.ofile, 'output/hello.txt')
        >>> b.run()

        Write a single job intermediate to a permanent location in GCS:

        >>> b = Batch()
        >>> j = b.new_job()
        >>> j.command(f'echo "hello" > {j.ofile}')
        >>> b.write_output(j.ofile, 'gs://mybucket/output/hello.txt')
        >>> b.run()  # doctest: +SKIP

        Write a single job intermediate to a permanent location in Azure:

        >>> b = Batch()
        >>> j = b.new_job()
        >>> j.command(f'echo "hello" > {j.ofile}')
        >>> b.write_output(j.ofile, 'https://my-account.blob.core.windows.net/my-container/output/hello.txt')
        >>> b.run()  # doctest: +SKIP

        .. warning::

            To avoid expensive egress charges, output files should be located in buckets
            that are multi-regional in the United States because Batch runs jobs in any
            US region.

        Notes
        -----
        All :class:`.JobResourceFile` are temporary files and must be written
        to a permanent location using :meth:`.write_output` if the output needs
        to be saved.

        Parameters
        ----------
        resource:
            Resource to be written to a file.
        dest:
            Destination file path. For a single :class:`.ResourceFile`, this will
            simply be `dest`. For a :class:`.ResourceGroup`, `dest` is the file
            root and each resource file will be written to `{root}.identifier`
            where `identifier` is the identifier of the file in the
            :class:`.ResourceGroup` map.
        """

        if not isinstance(resource, _resource.Resource):
            raise BatchException(f"'write_output' only accepts Resource inputs. Found '{type(resource)}'.")
        if (isinstance(resource, _resource.JobResourceFile)
                and isinstance(resource._source, job.BashJob)
                and resource not in resource._source._mentioned):
            name = resource._source._resources_inverse[resource]
            raise BatchException(f"undefined resource '{name}'\n"
                                 f"Hint: resources must be defined within the "
                                 f"job methods 'command' or 'declare_resource_group'")
        if (isinstance(resource, _resource.PythonResult)
                and isinstance(resource._source, job.PythonJob)
                and resource not in resource._source._mentioned):
            name = resource._source._resources_inverse[resource]
            raise BatchException(f"undefined resource '{name}'\n"
                                 f"Hint: resources must be bound as a result "
                                 f"using the PythonJob 'call' method")

        if isinstance(self._backend, _backend.LocalBackend):
            dest_scheme = url_scheme(dest)
            if dest_scheme == '':
                dest = os.path.abspath(os.path.expanduser(dest))

        resource._add_output_path(dest)

    def select_jobs(self, pattern: str) -> List[job.Job]:
        """
        Select all jobs in the batch whose name matches `pattern`.

        Examples
        --------

        Select jobs in batch matching `qc`:

        >>> b = Batch()
        >>> j = b.new_job(name='qc')
        >>> qc_jobs = b.select_jobs('qc')
        >>> assert qc_jobs == [j]

        Parameters
        ----------
        pattern:
            Regex pattern matching job names.
        """

        return [job for job in self._jobs if job.name is not None and re.match(pattern, job.name) is not None]

    @overload
    def run(self, dry_run: Literal[False] = ..., verbose: bool = ..., delete_scratch_on_exit: bool = ..., **backend_kwargs: Any) -> _bc.Batch: ...
    @overload
    def run(self, dry_run: Literal[True] = ..., verbose: bool = ..., delete_scratch_on_exit: bool = ..., **backend_kwargs: Any) -> None: ...
    def run(self,
            dry_run: bool = False,
            verbose: bool = False,
            delete_scratch_on_exit: bool = True,
            **backend_kwargs: Any) -> Optional[_bc.Batch]:
        """
        Execute a batch.

        Examples
        --------

        Create a simple batch with one job and execute it:

        >>> b = Batch()
        >>> j = b.new_job()
        >>> j.command('echo "hello world"')
        >>> b.run()


        Parameters
        ----------
        dry_run:
            If `True`, don't execute code.
        verbose:
            If `True`, print debugging output.
        delete_scratch_on_exit:
            If `True`, delete temporary directories with intermediate files.
        backend_kwargs:
            See :meth:`.Backend._run` for backend-specific arguments.
        """

        seen = set()
        ordered_jobs = []

        def schedule_job(j):
            if j in seen:
                return
            seen.add(j)
            for p in j._dependencies:
                schedule_job(p)
            ordered_jobs.append(j)

        for j in self._jobs:
            schedule_job(j)

        assert len(seen) == len(self._jobs)

        job_index = {j: i for i, j in enumerate(ordered_jobs, start=1)}
        for j in ordered_jobs:
            i = job_index[j]
            j._job_id = i
            for d in j._dependencies:
                if job_index[d] >= i:
                    raise BatchException("cycle detected in dependency graph")

        self._jobs = ordered_jobs
        run_result = self._backend._run(self, dry_run, verbose, delete_scratch_on_exit, **backend_kwargs)  # pylint: disable=assignment-from-no-return
        if self._DEPRECATED_fs is not None:
            # best effort only because this is deprecated
            async_to_blocking(self._DEPRECATED_fs.close())
            self._DEPRECATED_fs = None
        return run_result


    def __str__(self):
        return self._uid
