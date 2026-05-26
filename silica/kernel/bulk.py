from silica.driver import DRIVER
from silica.kernel import templates
from silica.kernel.ops import Op, OpType, FailedOp, BulkResult

def execute_operations(ops: list[Op]) -> BulkResult:
    results = []
    failed_ops = []
    success_count = 0
    
    for idx, op in enumerate(ops):
        op_type = op.op
        path = op.touched_ref()
        
        if op_type == OpType.skip:
            results.append({"index": idx, "op": "skip", "success": True})
            success_count += 1
            continue
            
        if not path:
            err_msg = "Missing 'path' parameter"
            failed_ops.append(FailedOp(index=idx, path="", op=op_type.value, error=err_msg))
            results.append({"index": idx, "success": False, "error": err_msg})
            continue
            
        try:
            if op_type == OpType.write:
                heading = op.heading
                snippet = op.snippet or ""
                hub = op.hub
                tags = op.tags
                related = op.related
                
                if not heading or not hub:
                    err_msg = "Missing 'heading' or 'hub' parameter for write operation"
                    failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=err_msg))
                    results.append({"index": idx, "path": path, "success": False, "error": err_msg})
                    continue
                    
                content = templates.template_spoke(
                    heading=heading,
                    snippet=snippet,
                    hub=hub,
                    tags=tags,
                    related=related
                )
                
                DRIVER.create(path, content)
                success_count += 1
                results.append({"index": idx, "path": path, "op": "write", "success": True})
                
            elif op_type == OpType.patch:
                heading = op.heading
                snippet = op.snippet
                source_basename = op.source_basename
                hub = op.hub

                if not heading or not snippet or not source_basename:
                    err_msg = "Missing 'heading', 'snippet', or 'source_basename' for patch operation"
                    failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=err_msg))
                    results.append({"index": idx, "path": path, "success": False, "error": err_msg})
                    continue

                try:
                    nc = DRIVER.read_note(path)
                    existing_content = nc.content
                except RuntimeError as e:
                    err_msg = f"Cannot patch; {e}"
                    failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=err_msg))
                    results.append({"index": idx, "path": path, "success": False, "error": err_msg})
                    continue

                new_content = templates.patch_snippet(
                    heading=heading,
                    snippet=snippet,
                    source_basename=source_basename,
                    hub=hub,
                    existing_content=existing_content
                )

                DRIVER.overwrite(path, new_content)
                success_count += 1
                results.append({"index": idx, "path": path, "op": "patch", "success": True})
                
            elif op_type == OpType.overwrite:
                content = op.content
                if content is None:
                    err_msg = "Missing 'content' for overwrite operation"
                    failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=err_msg))
                    results.append({"index": idx, "path": path, "success": False, "error": err_msg})
                    continue
                DRIVER.overwrite(path, content)
                success_count += 1
                results.append({"index": idx, "path": path, "op": "overwrite", "success": True})
                
            elif op_type == OpType.delete:
                DRIVER.delete(path)
                success_count += 1
                results.append({"index": idx, "path": path, "op": "delete", "success": True})
                
            else:
                err_msg = f"Unknown operation type: {op_type}"
                failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=err_msg))
                results.append({"index": idx, "path": path, "success": False, "error": err_msg})
                
        except Exception as e:
            failed_ops.append(FailedOp(index=idx, path=path, op=op_type.value, error=str(e)))
            results.append({"index": idx, "path": path, "success": False, "error": str(e)})

    ok = len(failed_ops) == 0
    return BulkResult(
        ok=ok,
        failed=failed_ops,
        results=results,
        total=len(ops),
        successful=success_count
    )
