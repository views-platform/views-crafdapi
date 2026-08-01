import ast
import sys
import re
import pyprojroot
from typing import Union, Optional, Dict
import logging
import importlib
import hashlib
from pathlib import Path
import random

from abc import abstractmethod
from views_crafdapi.managers.log import LoggingModule

logger = logging.getLogger(__name__)

_BLOCKED_MODULES = frozenset({
    "os", "subprocess", "shutil", "socket", "http", "urllib",
    "ctypes", "code", "codeop", "pty", "pipes", "signal",
})

_BLOCKED_CALLS = frozenset({
    "exec", "eval", "compile", "__import__", "breakpoint",
    "exit", "quit", "open",
})


# ============================================================ Model Path Manager ============================================================


class ModelPathManager:
    """
    A class to manage model paths and directories within the ViEWS Pipeline.

    Attributes:
        __instances__ (int): A class-level counter to track the number of ModelPathManager instances.
        model_name (str): The name of the model.
        _validate (bool): A flag to indicate whether to validate paths and names.
        target (str): The target type (e.g., 'model').
        root (Path): The root directory of the project.
        models (Path): The directory for models.
        model_dir (Path): The directory for the specific model.
        artifacts (Path): The directory for model artifacts.
        configs (Path): The directory for model configurations.
        data (Path): The directory for model data.
        data_generated (Path): The directory for generated data.
        data_processed (Path): The directory for processed data.
        data_raw (Path): The directory for raw data.
        reports (Path): The directory for reports.
        queryset_path (Path): The path to the queryset script.
        _queryset (module): The imported queryset module.
        scripts (list): A list of script paths.
        _ignore_attributes (list): A list of paths to ignore.
    """

    _target = "model"
    __instances__ = 0
    _root = None

    @classmethod
    def _initialize_class_paths(cls, current_path: Path = None) -> None:
        """Initialize class-level paths."""
        cls._root = cls.find_project_root(current_path=current_path)

    @classmethod
    def get_root(cls, current_path: Path = None) -> Path:
        """Get the root path."""
        if cls._root is None:
            cls._initialize_class_paths(current_path=current_path)
        return cls._root

    @classmethod
    def get_models(cls) -> Path:
        """Get the models path."""
        if cls._root is None:
            cls._initialize_class_paths()
        return cls._root / Path(cls._target + "s")

    @classmethod
    def check_if_model_dir_exists(cls, model_name: str) -> bool:
        """
        Check if the model directory exists.

        Args:
            cls (type): The class calling this method.
            model_name (str): The name of the model.

        Returns:
            bool: True if the model directory exists, False otherwise.
        """
        model_dir = cls.get_models() / model_name
        return model_dir.exists()

    @staticmethod
    def generate_hash(model_name: str, validate: bool, target: str) -> str:
        """
        Generates a unique hash for the ModelPathManager instance.

        Args:
            model_name (str or Path): The model name.
            validate (bool): Whether to validate paths and names.
            target (str): The target type (e.g., 'model').

        Returns:
            str: The SHA-256 hash of the model name, validation flag, and target.
        """
        return hashlib.sha256(str((model_name, validate, target)).encode()).hexdigest()

    @staticmethod
    def get_model_name_from_path(path: Union[Path, str]) -> str:
        """
        Extracts the model or ensemble name from a path containing exactly one of 'models' or 'ensembles'.

        Args:
            path (Union[Path, str]): The path to analyze (typically from `Path(__file__)`).

        Returns:
            str: The validated model/ensemble name if found, otherwise None.

        Example:
            >>> get_model_name_from_path("project/models/my_model/script.py")
            "my_model"
        """
        path = Path(path)
        logger.debug(f"Extracting model name from path: {path}")

        # Define valid parent directories and check for exactly one occurrence

        valid_parents = {"models", "ensembles", "preprocessors", "postprocessors", "extractors", "apis"}

        found_parents = [parent for parent in valid_parents if parent in path.parts]

        if len(found_parents) != 1:
            logger.debug(
                f"Path must contain exactly one of {valid_parents}. Found: {found_parents}"
            )
            return None

        parent_dir = found_parents[0]
        parent_idx = path.parts.index(parent_dir)

        # Check if there's a subdirectory after the parent directory
        if parent_idx + 1 >= len(path.parts):
            logger.debug(
                f"No name found after '{parent_dir}' directory in path: {path}"
            )
            return None

        model_name = path.parts[parent_idx + 1]

        # Validate and return the extracted name
        if ModelPathManager.validate_model_name(model_name):
            logger.debug(
                f"Valid {parent_dir[:-1]} name '{model_name}' found in path: {path}"
            )
            return model_name
        else:
            logger.debug(
                f"Invalid name '{model_name}' after '{parent_dir}' directory in path: {path}"
            )
            return None

    @staticmethod
    def validate_model_name(name: str) -> bool:
        """
        Validates the model name to ensure it follows the lowercase "adjective_noun" format.

        Parameters:
            name (str): The model name to validate.

        Returns:
            bool: True if the name is valid, False otherwise.
        """
        # Define a basic regex pattern for a noun_adjective format
        pattern = r"^[a-z]+_[a-z]+$"
        # Check if the name matches the pattern
        if re.match(pattern, name):
            # You might want to add further checks for actual noun and adjective validation
            # For now, this regex checks for two words separated by an underscore
            return True
        return False

    @staticmethod
    def find_project_root(current_path: Path = None, marker=".gitignore") -> Path:
        """
        Finds the base directory of the project by searching for a specific marker file or directory.
        Args:
            marker (str): The name of the marker file or directory that indicates the project root.
                        Defaults to '.gitignore'.
        Returns:
            Path: The path of the project root directory.
        Raises:
            FileNotFoundError: If the marker file/directory is not found up to the root directory.
        """
        if current_path is None:
            current_path = Path(pyprojroot.here())
            if (current_path / marker).exists():
                return current_path
        # Start from the current directory and move up the hierarchy
        try:
            current_path = Path(current_path).resolve().parent
            while current_path != current_path.parent:
                if (current_path / marker).exists():
                    return current_path
                current_path = current_path.parent
        except (OSError, ValueError) as e:
            raise FileNotFoundError(
                f"{marker} not found in the directory hierarchy. "
                f"Unable to find project root. {current_path}"
            ) from e
        raise FileNotFoundError(
            f"{marker} not found in the directory hierarchy. "
            f"Searched from {current_path} to filesystem root."
        )

    def __init__(self, model_path: Union[str, Path], validate: bool = True) -> None:
        """
        Initializes a ModelPathManager instance.

        Args:
            model_path (str or Path): The model name or path.
            validate (bool, optional): Whether to validate paths and names. Defaults to True.
            target (str, optional): The target type (e.g., 'model'). Defaults to 'model'.
        """

        # Configs
        self.__class__.__instances__ += 1

        self._validate = validate
        self.target = self.__class__._target

        # Common paths
        self.root = self.__class__.get_root()
        self.models = self.__class__.get_models()
        # Ignore attributes while processing
        self._ignore_attributes = [
            "model_name",
            "model_dir",
            "scripts",
            "_validate",
            "models",
            "_sys_paths",
            "queryset_path",
            "_queryset",
            "_ignore_attributes",
            "target",
            "_instance_hash",
        ]

        self.model_name = self._process_model_name(model_path)
        self._instance_hash = self.generate_hash(
            self.model_name, self._validate, self.target
        )
        self.dotenv = self.root / ".env"
        self._initialize_directories()
        self._initialize_scripts()
        logger.debug(
            f"ModelPathManager instance {ModelPathManager.__instances__} initialized for {self.model_name}."
        )

    def _process_model_name(self, model_path: Union[str, Path]) -> str:
        """
        Processes the input model name or path and returns a valid model name.

        If the input is a path, it extracts the model name from the path.
        If the input is a model name, it validates the name format.

        Args:
            model_path (Union[str, Path]): The model name or path to process.

        Returns:
            str: The processed model name.

        Raises:
            ValueError: If the model name is invalid.

        Example:
            >>> self._process_model_name("models/my_model")
            'my_model'
        """
        # Should fail as violently as possible if the model name is invalid.
        if self._is_path(model_path, validate=self._validate):
            logger.debug(f"Path input detected: {model_path}")
            try:
                result = self.get_model_name_from_path(model_path)
                if result:
                    logger.debug(f"Model name extracted from path: {result}")
                    return result
                else:
                    raise ValueError(
                        f"Invalid {self.target} name. Please provide a valid {self.target} name that follows the lowercase 'adjective_noun' format. Path given: {model_path}"
                    )
            except Exception as e:
                logger.error(
                    f"Error extracting model name from path: {e}", exc_info=True
                )
                raise
        else:
            if not self.validate_model_name(model_path):
                raise ValueError(
                    f"Invalid {self.target} name. Please provide a valid {self.target} name that follows the lowercase 'adjective_noun' format. Path given: {model_path}"
                )
            logger.debug(f"{self.target.title()} name detected: {model_path}")
            return model_path

    def _initialize_directories(self) -> None:
        """
        Initializes the necessary directories for the model.

        Creates and sets up various directories required for the model, such as architectures, artifacts, configs, data, etc.
        """
        self.model_dir = self._get_model_dir()
        self.logging = self.model_dir / "logs"
        self.artifacts = self._build_absolute_directory(Path("artifacts"))
        self.configs = self._build_absolute_directory(Path("configs"))
        self.data = self._build_absolute_directory(Path("data"))
        self.data_generated = self._build_absolute_directory(Path("data/generated"))
        self.data_processed = self._build_absolute_directory(Path("data/processed"))
        self.reports = self._build_absolute_directory(Path("reports"))

    def _initialize_scripts(self) -> None:
        """
        Initializes the necessary scripts for the model.

        Creates and sets up various scripts required for the model, such as configuration scripts, main script, and other utility scripts.
        """
        self.scripts = [
            self._build_absolute_directory(Path("configs/config_deployment.py")),
            self._build_absolute_directory(Path("configs/config_hyperparameters.py")),
            self._build_absolute_directory(Path("configs/config_meta.py")),
            self._build_absolute_directory(Path("main.py")),
            self._build_absolute_directory(Path("README.md")),
        ]

    @staticmethod
    def _is_path(path_input: Union[str, Path], validate: bool = True) -> bool:
        """
        Determines if the given input is a valid path.

        This method checks if the input is a string or a Path object and verifies if it points to an existing file or directory.

        Args:
            path_input (Union[str, Path]): The input to check.
            validate (bool, optional): Whether to check if the path exists. Defaults to True.

        Returns:
            bool: True if the input is a valid path, False otherwise.
        """
        try:
            path_input = Path(path_input) if isinstance(path_input, str) else path_input
            if validate:
                return path_input.exists() and len(path_input.parts) > 1
            else:
                return len(path_input.parts) > 1
            # return path_input.exists() and len(path_input.parts) > 1
        except Exception as e:
            logger.error(f"Error checking if input is a path: {e}")
            return False

    def _get_model_dir(self) -> Path:
        """
        Determines the model directory based on validation.

        This method constructs the model directory path and checks if it exists.
        If the directory does not exist and validation is enabled, it raises a FileNotFoundError.

        Returns:
            Path: The model directory path.

        Raises:
            FileNotFoundError: If the model directory does not exist and validation is enabled.
        """
        model_dir = self.models / self.model_name
        if not self._check_if_dir_exists(model_dir) and self._validate:
            error = f"{self.target.title()} directory {model_dir} does not exist. Please create it first using `make_new_model.py` or set validate to `False`."
            logger.error(error, exc_info=True)
            raise FileNotFoundError(error)
        return model_dir

    def _check_if_dir_exists(self, directory: Path) -> bool:
        """
        Checks if the directory already exists.
        Args:
            directory (Path): The directory path to check.
        Returns:
            bool: True if the directory exists, False otherwise.
        """
        return directory.exists()

    def _build_absolute_directory(self, directory: Path) -> Path:
        """
        Build an absolute directory path based on the model directory.
        """
        directory = self.model_dir / directory
        if self._validate:
            if not self._check_if_dir_exists(directory=directory):
                logger.warning(f"Directory {directory} does not exist. Continuing...")
                if directory.name.endswith(".py"):
                    return directory.name
                return None
        return directory

    def view_directories(self) -> None:
        """
        Prints a formatted list of the directories and their absolute paths.

        This method iterates through the instance's attributes and prints the name and path of each directory.
        It ignores certain attributes specified in the _ignore_attributes list.
        """
        print("\n{:<20}\t{:<50}".format("Name", "Path"))
        print("=" * 72)
        for attr, value in self.__dict__.items():
            # value = getattr(self, attr)
            if attr not in self._ignore_attributes and isinstance(value, Path):
                print("{:<20}\t{:<50}".format(str(attr), str(value)))

    def view_scripts(self) -> None:
        """
        Prints a formatted list of the scripts and their absolute paths.

        This method iterates through the scripts attribute and prints the name and path of each script.
        If a script path is None, it prints "None" instead of the path.
        """
        print("\n{:<20}\t{:<50}".format("Script", "Path"))
        print("=" * 72)
        for path in self.scripts:
            if isinstance(path, Path):
                print("{:<20}\t{:<50}".format(str(path.name), str(path)))
            else:
                print("{:<20}\t{:<50}".format(str(path), "None"))

    def get_directories(self) -> Dict[str, Optional[str]]:
        """
        Retrieve a dictionary of directory names and their paths.

        Returns:
            dict: A dictionary where keys are directory names and values are their paths.
        """
        directories = {}
        relative = False
        for attr, value in self.__dict__.items():

            if str(attr) not in [
                "model_name",
                "root",
                "scripts",
                "_validate",
                "models",
                "templates",
                "_sys_paths",
                "_queryset",
                "queryset_path",
                "_ignore_attributes",
                "target",
                "_force_cache_overwrite",
                "initialized",
                "_instance_hash",
            ] and isinstance(value, Path):
                if not relative:
                    directories[str(attr)] = str(value)
                else:
                    if self.model_name in value.parts:
                        relative_path = value.relative_to(self.model_dir)
                    else:
                        relative_path = value
                    if relative_path == Path("."):
                        continue
                    directories[str(attr)] = str(relative_path)
        return directories

    def get_scripts(self) -> Dict[str, Optional[str]]:
        """
        Returns a dictionary of the scripts and their absolute paths.

        Returns:
            dict: A dictionary containing the scripts and their absolute paths.
        """
        scripts = {}
        relative = False
        for path in self.scripts:
            if isinstance(path, Path):
                if relative:
                    if self.model_dir in path.parents:
                        scripts[str(path.name)] = str(path.relative_to(self.model_dir))
                    else:
                        scripts[str(path.name)] = str(path)
                else:
                    scripts[str(path.name)] = str(path)
            else:
                scripts[str(path)] = None
        return scripts


# ============================================================ Model Manager ============================================================


class ModelManager:
    """
    Manages the basic initialization of a model, including configuration loading, format setting and storage settings.

    Attributes:
        _entity (str): The WandB entity name.
        _model_path (ModelPathManager): The path manager for the model.
        _script_paths (dict): Dictionary of script paths.
        _config_deployment (dict): Deployment configuration.
        _config_hyperparameters (dict): Hyperparameters configuration.
        _config_meta (dict): Metadata configuration.
        _config_sweep (dict): Sweep configuration (if applicable).
        _data_loader (ViewsDataLoader): Data loader for fetching and preprocessing data.
    """

    __instances__ = 0

    def __init__(
        self,
        model_path: ModelPathManager,
        wandb_notifications: bool = False,
    ) -> None:
        """
        Initializes the ModelManager with the given model path.

        Args:
            model_path (ModelPathManager): The path manager for the model.
        """
        self.__class__.__instances__ += 1

        self._model_repo = "views-models"
        self._entity = "views_pipeline"

        self._model_path = model_path
        self._wandb_notifications = wandb_notifications
        self._logger = LoggingModule(logging_path=self._model_path.logging).get_logger()

        self._script_paths = self._model_path.get_scripts()
        self._config_deployment = self.__load_config(
            "config_deployment.py", "get_deployment_config"
        )
        self._config_hyperparameters = self.__load_config(
            "config_hyperparameters.py", "get_hp_config"
        )
        self._config_meta = self.__load_config("config_meta.py", "get_meta_config")
        if self.__class__.__instances__ == 1:
            self.__ascii_splash()

    def __ascii_splash(self) -> None:
        from art import text2art

        text = text2art(
            f"{self._model_path.model_name.replace('-', ' ')}", font="random-medium"
        )

        colored_text = "".join(
            [f"\033[{random.choice(range(31, 37))}m{char}\033[0m" for char in text]
        )
        print(colored_text)

    @staticmethod
    def _validate_config_ast(script_path: str) -> bool:
        """Check a config file's AST for dangerous constructs before execution."""
        try:
            source = Path(script_path).read_text()
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_module = alias.name.split(".")[0]
                    if root_module in _BLOCKED_MODULES:
                        logger.warning(
                            "Config %s imports blocked module: %s",
                            script_path, alias.name,
                        )
                        return False
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_module = node.module.split(".")[0]
                    if root_module in _BLOCKED_MODULES:
                        logger.warning(
                            "Config %s imports from blocked module: %s",
                            script_path, node.module,
                        )
                        return False
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
                    logger.warning(
                        "Config %s calls blocked function: %s",
                        script_path, node.func.id,
                    )
                    return False
        return True

    def __load_config(self, script_name: str, config_method: str) -> Union[Dict, None]:
        """
        Loads and executes a configuration method from a specified script.

        Args:
            script_name (str): The name of the script to load.
            config_method (str): The name of the configuration method to execute.

        Returns:
            dict: The result of the configuration method if the script and method are found, otherwise None.

        Raises:
            AttributeError: If the specified configuration method does not exist in the script.
            ImportError: If there is an error importing the script.
        """
        script_path = self._script_paths.get(script_name)
        if script_path:
            if not self._validate_config_ast(script_path):
                logger.error(
                    "Config %s failed AST safety check — refusing to execute",
                    script_name,
                )
                return None
            try:
                spec = importlib.util.spec_from_file_location(script_name, script_path)
                config_module = importlib.util.module_from_spec(spec)
                sys.modules[script_name] = config_module
                spec.loader.exec_module(config_module)
                if hasattr(config_module, config_method):
                    return getattr(config_module, config_method)()
            except (AttributeError, ImportError, FileNotFoundError, OSError) as e:
                logger.warning(
                    f"Config {script_name} not loadable: {e}"
                )
                return None

        return None
    
    @property
    def configs(self) -> Dict:
        """
        Get the combined meta, deployment and hyperparameters configuration.

        Returns:
            dict: The configuration object.
        """

        # config = {
        #     **self._config_hyperparameters,
        #     **self._config_meta,
        #     **self._config_deployment,
        # }
        config = {}
        if hasattr(self, "_config_hyperparameters") and self._config_hyperparameters is not None:
            config.update(self._config_hyperparameters)
        if hasattr(self, "_config_deployment") and self._config_deployment is not None:
            config.update(self._config_deployment)
        if hasattr(self, "_config_meta") and self._config_meta is not None:
            config.update(self._config_meta)

        return config
    
class APIPathManager(ModelPathManager):
    """
    A class to manage API paths and directories within the ViEWS Pipeline.

    Attributes:
        target (str): The target type set to 'api'.
        endpoints (Path): The directory for API endpoints.
        middleware (Path): The directory for API middleware.
        schemas (Path): The directory for API schemas/validation.
        tests (Path): The directory for API tests.
        docs (Path): The directory for API documentation.
    """

    _target = "api"

    def __init__(self, api_path: Union[str, Path], validate: bool = True) -> None:
        """
        Initializes an APIPathManager instance.

        Args:
            api_path (str or Path): The API name or path.
            validate (bool, optional): Whether to validate paths and names. Defaults to True.
        """
        super().__init__(api_path, validate)
        self._initialize_api_specific_directories()
        self._initialize_api_specific_scripts()
        
    def _initialize_api_specific_directories(self) -> None:
        """Initialize API-specific directories."""
        self.cache = self._build_absolute_directory(Path("cache"))
        
    def _initialize_api_specific_scripts(self) -> None:
        """Initialize and append API-specific script paths."""
        pass

    def get_latest_api_artifact_path(self, artifact_type: str) -> Path:
        """
        Retrieve the path to the latest API artifact for a given type.

        Args:
            artifact_type (str): The type of artifact (e.g., 'swagger', 'openapi', 'docs').

        Returns:
            Path: The path to the latest API artifact.

        Raises:
            FileNotFoundError: If no API artifacts are found for the given type.
        """
        common_extensions = [".json", ".yaml", ".yml", ".html", ".md"]
        artifact_files = [
            f
            for f in self.artifacts.iterdir()
            if f.is_file()
            and f.stem.startswith(f"{artifact_type}_")
            and f.suffix in common_extensions
        ]
        
        if not artifact_files:
            raise FileNotFoundError(
                f"No API artifacts found for type '{artifact_type}' in path '{self.artifacts}'"
            )
            
        artifact_files.sort(reverse=True)
        logger.info(f"API artifact used: {artifact_files[0]}")
        return self.artifacts / artifact_files[0]

class APIManager(ModelManager):
    """
    Manages the API lifecycle activities including startup, shutdown, and maintenance.

    Attributes:
        _api_path (APIPathManager): The path manager for the API.
        _config_api (dict): API configuration.
        _config_endpoints (dict): Endpoints configuration.
        _config_middleware (dict): Middleware configuration.
    """

    def __init__(
        self,
        model_path: APIPathManager,
        wandb_notifications: bool = False,
    ) -> None:
        """
        Initializes the APIManager with the given API path.

        Args:
            model_path (APIPathManager): The path manager for the API.
            wandb_notifications (bool, optional): Enable or disable Weights & Biases notifications. Defaults to False.
        """
        super().__init__(
            model_path=model_path,
            wandb_notifications=wandb_notifications,
        )
        
        # Load API-specific configurations
        self._api_server = None
        self._is_running = False


    @abstractmethod
    def _startup(self):
        """Initialize and start the API server."""
        pass

    @abstractmethod 
    def _shutdown(self):
        """Gracefully shutdown the API server."""
        pass

    @abstractmethod
    def _health_check(self):
        """Perform health checks on the API server."""
        pass

    @abstractmethod
    def _maintenance(self):
        """Perform maintenance tasks on the API."""
        pass

    def run(self):
        """
        Main entry point for API lifecycle management.
        Reads the action from self.configs to determine what operation to perform.
        """
        import wandb
        from views_crafdapi.wandb.utils import wandb_alert

        action = self.configs.get('action')

        if not action:
            logger.error("No action specified in configs for API management")
            return

        action = action.lower()

        with wandb.init(
            project=f"{self.configs['name']}_api", 
            entity=self._entity, 
            job_type=f"api_{action}"
        ):
            try:
                if action == "start":
                    self._startup()
                    self._is_running = True
                elif action == "stop":
                    self._shutdown()
                    self._is_running = False
                elif action == "health":
                    self._health_check()
                elif action == "maintenance":
                    self._maintenance()
                else:
                    logger.warning(f"Unknown action: {action}")
                    
            except Exception as e:
                logger.error(f"Error during API {action}: {e}")
                self._shutdown()
                wandb_alert(
                    title=f"API {action} failed.",
                    text=f"Error details: {e}",
                    wandb_notifications=self._wandb_notifications,
                    models_path=self._model_path.models
                )
            finally:
                wandb.finish()