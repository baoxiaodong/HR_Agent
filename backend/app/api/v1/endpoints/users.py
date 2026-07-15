"""
用户与角色管理 API 端点。

普通用户接口通过 ``get_current_user`` 验证身份，并在服务层继续判断是否有权查看或修改
目标用户；``/admin`` 接口通过 ``get_current_admin_by_role`` 提前拦截非管理员请求。
用户资料由 ``UserService`` 管理，角色及用户-角色关联由 ``RoleService`` 管理。
"""
from typing import Any, List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.user import User as UserSchema, UserUpdate, UserCreate, Role as RoleSchema, RoleCreate, AssignRolesRequest, UserWithRoles
from app.services.user_service import UserService, RoleService
from app.api.deps import get_current_user, get_current_admin_by_role
from app.models.user import UserRole

router = APIRouter()


@router.get("/", response_model=List[UserWithRoles])
async def get_users(
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = None,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员分页列出用户，或按关键词搜索并装配角色。

    搜索分支先由服务校验 HR/管理员角色，再逐用户查询角色并构造 ``UserWithRoles``；无搜索
    分支直接使用服务的批量角色装配。认证依赖与服务角色枚举共同形成权限边界。
    """
    user_service = UserService(db)

    # 如果有搜索关键字，使用搜索功能
    if search:
        try:
            search_results = await user_service.search_users(search, current_user, limit)
            # 转换搜索结果为UserWithRoles格式
            result = []
            for user in search_results:
                # 获取用户的角色信息
                role_service = RoleService(db)
                user_roles = await role_service.list_user_roles(user.id)

                # 构造返回数据
                user_data = {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "phone": user.phone,
                    "department": user.department,
                    "position": user.position,
                    "employee_id": user.employee_id,
                    "role": user.role,
                    "is_superuser": user.is_superuser,
                    "is_verified": user.is_verified,
                    "is_active": user.is_active,
                    "bio": user.bio,
                    "avatar_url": user.avatar_url,
                    "last_login": user.last_login,
                    "created_at": user.created_at,
                    "updated_at": user.updated_at,
                    "roles": [
                        {
                            "id": r.id,
                            "name": r.name,
                            "description": r.description,
                            "is_builtin": r.is_builtin,
                            "created_at": r.created_at,
                            "updated_at": r.updated_at,
                        }
                        for r in user_roles
                    ],
                }
                result.append(user_data)
            return result
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足"
            )
    else:
        # 否则返回所有用户
        result = await user_service.get_users_with_roles(skip=skip, limit=limit)
        return result


@router.get("/{user_id}", response_model=UserSchema)
async def get_user(
    user_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """读取包含停用账号在内的目标用户，再校验查看权限。

    路径字符串先转为 UUID；普通用户只能查看自己，超级用户可查看任意账号。资源不存在返回
    404，存在但无权访问返回 403。
    """
    user_service = UserService(db)
    user = await user_service.get_user(UUID(user_id), include_inactive=True)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户未找到"
        )

    # 检查权限
    if not user_service.can_view_user(current_user, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足"
        )

    return user


@router.put("/{user_id}", response_model=UserSchema)
async def update_user(
    user_id: str,
    user_update: UserUpdate,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """读取目标账号并在权限校验后执行局部资料更新。

    普通用户只能修改自己，超级用户可修改任意账号；服务层排除明文密码字段、单独哈希新密码，
    检查邮箱/用户名冲突并提交显式更新字段。
    """
    user_service = UserService(db)
    user = await user_service.get_user(UUID(user_id), include_inactive=True)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户未找到"
        )

    # 检查权限
    if not user_service.can_update_user(current_user, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足"
        )

    updated_user = await user_service.update_user(user.id, user_update, current_user)
    return updated_user


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员停用目标用户，而不是物理删除记录。

    管理员依赖先完成角色校验，端点再执行超级用户权限判断；服务层把 ``is_active`` 更新为
    ``False`` 并提交。已停用或不存在的目标统一表现为 404。
    """
    user_service = UserService(db)
    user = await user_service.get_user(UUID(user_id))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户未找到"
        )

    # 检查权限
    if not user_service.can_delete_user(current_user, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足"
        )

    result = await user_service.delete_user(user.id, current_user)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="删除用户失败"
        )

    return {"message": "用户删除成功"}

# Admin-only user management

@router.post("/admin/users", response_model=UserSchema)
async def admin_create_user(
    user_create: UserCreate,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员创建用户并尝试分配默认角色。

    服务层检查唯一字段、哈希密码并先提交用户，随后以第二个事务创建默认角色关联；后者失败
    不撤销已创建用户，因此可能形成无默认角色账号。
    """
    user_service = UserService(db)
    user = await user_service.create_user(user_create)
    return user


@router.get("/admin/users/{user_id}/roles", response_model=List[RoleSchema])
async def admin_list_user_roles(
    user_id: str,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员列出目标用户关联的全部启用角色。

    路径 ID 转为 UUID 后直接查询关联表；用户不存在或没有角色时服务返回空列表，不在端点
    额外区分这两种状态。
    """
    role_service = RoleService(db)
    roles = await role_service.list_user_roles(UUID(user_id))
    return roles


@router.put("/admin/users/{user_id}/roles", response_model=List[RoleSchema])
async def admin_assign_user_roles(
    user_id: str,
    payload: AssignRolesRequest,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员用请求中的角色集合替换目标用户现有角色。

    服务层先验证用户和每个角色，再提交旧关联删除，随后第二次提交新关联；两阶段不是原子
    事务，第二次提交失败时用户可能保留为空角色状态。
    """
    role_service = RoleService(db)
    roles = await role_service.assign_roles_to_user(UUID(user_id), payload.role_ids)
    return roles


# Admin-only role management

@router.get("/admin/roles", response_model=List[RoleSchema])
async def admin_list_roles(
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员按创建时间倒序列出全部启用角色。

    端点不接受分页或筛选参数，查询和数据库异常处理全部由 ``RoleService`` 负责。
    """
    role_service = RoleService(db)
    return await role_service.list_roles()


@router.post("/admin/roles", response_model=RoleSchema)
async def admin_create_role(
    role_create: RoleCreate,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员创建名称唯一的角色并提交。

    请求 Schema 先校验名称、描述和内置标志；服务层检查重名、提交并刷新 ORM，失败时回滚。
    """
    role_service = RoleService(db)
    return await role_service.create_role(role_create.name, role_create.description, role_create.is_builtin or False)


@router.delete("/admin/roles/{role_id}")
async def admin_delete_role(
    role_id: str,
    current_user: UserSchema = Depends(get_current_admin_by_role),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """由管理员物理删除指定角色。

    服务层按 UUID 读取并提交 ORM 删除；未命中返回 404。角色关联的清理或外键限制由数据库
    模型配置决定，端点不执行额外补偿。
    """
    role_service = RoleService(db)
    ok = await role_service.delete_role(UUID(role_id))
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="角色未找到")
    return {"message": "角色删除成功"}

@router.get("/me/roles", response_model=List[RoleSchema])
async def get_my_roles(
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """返回当前认证用户关联的全部启用角色。

    用户 ID 只取自认证上下文，不接受客户端指定；服务层联结角色关联表并按角色创建时间倒序。
    """
    role_service = RoleService(db)
    return await role_service.list_user_roles(current_user.id)
