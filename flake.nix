{
  description = "Development environment for claude-code-discord-bot-hooks";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # fenix = {
    #   url = "github:nix-community/fenix";
    #   inputs.nixpkgs.follows = "nixpkgs";
    # };
    devshell.url = "github:numtide/devshell";
  };
  outputs = { nixpkgs, fenix, devshell, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-darwin" ];
      pkgsFor = system: import nixpkgs {
        inherit system; overlays = [
        #fenix.overlays.default 
        devshell.overlays.default
      ];
      };

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.devshell.mkShell {
            packages = with pkgs; [
              stdenv
              fish
              python3
              uv
              cairo
            ];
            commands = [
              {
                name = "claude-qwen3.6-nix";
                command = ''
                  ANTHROPIC_BASE_URL=http://localhost:8001 \
                  CLAUDE_CODE_ATTRIBUTION_HEADER="0" \
                  ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3.6 \
                  ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3.6 \
                  ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3.6 \
                  claude
                '';
              }
            ];
          };
        }
      );
    };
}
