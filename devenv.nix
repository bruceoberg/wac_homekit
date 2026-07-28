{ pkgs, config, ... }:

{
  packages = with pkgs;
  [
    just    # run stuff in the justfile
  ];

  languages.python =
  {
    enable = true;
    # to use, run this first:
    #  devenv inputs add nixpkgs-python github:cachix/nixpkgs-python --follows nixpkgs
    #version = "3.13";
    venv.enable = true;

    uv =
    {
      enable = true;
      sync.enable = true;  # Auto-sync dependencies on direnv reload
    };
  };

  # Point uv at devenv's managed venv so `uv run` and the shell agree on
  # which Python / site-packages they're using.
  env.UV_PROJECT_ENVIRONMENT = "${config.devenv.root}/.devenv/state/venv";

  # Uncomment if a C extension needs a shared library on the load path.
  # env.LD_LIBRARY_PATH   = lib.makeLibraryPath [ pkgs.somelib ];  # Linux
  # env.DYLD_LIBRARY_PATH = lib.makeLibraryPath [ pkgs.somelib ];  # macOS

  # See full reference at https://devenv.sh/reference/options/
}
