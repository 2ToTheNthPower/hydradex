-- Copy to ~/.config/nvim/lua/plugins/hydradex.lua (Neovim >= 0.11).
return {
	"neovim/nvim-lspconfig",
	opts = {
		servers = {
			hydradex = {
				mason = false,
				cmd = { "hydradex", "--stdio" },
				filetypes = { "yaml" },
				root_markers = { "pyproject.toml", ".git" },
				settings = {
					hydradex = {
						matchFilter = "top matches only",
						isolateWorkspaceFolders = true,
						-- pythonPath = "/absolute/path/to/project/.venv/bin/python",
						-- extraPaths = { "src" },
						-- configRoots = { "conf" },
					},
				},
			},
		},
		setup = {
			hydradex = function(_, opts)
				-- Register the custom server directly; no upstream lspconfig entry needed.
				opts.init_options = opts.settings.hydradex
				vim.lsp.config("hydradex", opts)
				vim.lsp.enable("hydradex")
				return true
			end,
		},
	},
}
