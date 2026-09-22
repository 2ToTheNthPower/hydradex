local root, python, config_path = unpack(arg)
local spec = dofile(config_path)
local opts = spec.opts.servers.hydradex
opts.cmd = { python, "-m", "hydradex", "--stdio" }
assert(spec.opts.setup.hydradex("hydradex", opts))

vim.cmd("edit " .. vim.fn.fnameescape(root .. "/config.yaml"))
vim.bo.filetype = "yaml"
local client
assert(vim.wait(15000, function()
  client = vim.lsp.get_clients({ bufnr = 0, name = "hydradex" })[1]
  return client and client.initialized
end, 20), "HydraDex did not attach")

local uri = vim.uri_from_bufnr(0)
local response = client:request_sync("textDocument/definition", {
  textDocument = { uri = uri },
  position = { line = 1, character = 20 },
}, 15000, 0)
assert(response and not response.err, vim.inspect(response))
assert(response.result[1].uri == vim.uri_from_fname(root .. "/models.py"), vim.inspect(response))

response = client:request_sync("textDocument/completion", {
  textDocument = { uri = uri },
  position = { line = 2, character = 2 },
}, 15000, 0)
assert(response and not response.err, vim.inspect(response))
local labels = {}
for _, item in ipairs(response.result) do
  labels[item.label] = true
end
assert(labels.width and labels.bias, vim.inspect(response))

-- Exercise an unsaved override whose Python target lives in a defaults file.
local published_version
client.handlers["textDocument/publishDiagnostics"] = function(err, result, ctx, config)
  if result and result.uri == uri then
    published_version = result.version
  end
  vim.lsp.handlers["textDocument/publishDiagnostics"](err, result, ctx, config)
end
vim.api.nvim_buf_set_lines(0, 0, -1, false, { "defaults: [base]", "model:", "  bi" })
local version = vim.lsp.util.buf_versions[vim.api.nvim_get_current_buf()]
assert(vim.wait(15000, function() return published_version == version end, 20), "Edit not synchronized")
response = client:request_sync("textDocument/completion", {
  textDocument = { uri = uri },
  position = { line = 2, character = 4 },
}, 15000, 0)
assert(response and not response.err, vim.inspect(response))
assert(response.result[1].label == "bias", vim.inspect(response))
client:stop()
assert(vim.wait(10000, function() return client:is_stopped() end, 20), "Server failed to stop")
vim.cmd("qa!")
